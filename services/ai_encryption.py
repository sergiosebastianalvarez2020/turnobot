"""
services/ai_encryption.py — Cifrado de credenciales de proveedores de IA.

Cifrado AES-256-GCM con clave derivada de variables de entorno independientes
(no se deriva de SECRET_KEY). Soporta versionado de claves para rotación segura:

- AI_ENCRYPTION_KEY: clave vigente (se usa para cifrar nuevas credenciales).
- AI_ENCRYPTION_KEY_PREVIOUS: clave anterior (solo se usa para descifrar
  credenciales cifradas con la versión anterior durante la transición).

Cada cifra usa un nonce aleatorio de 96 bits prependido al criptograma.
El resultado se codifica en base64 para almacenamiento en TEXT de la BD.

Nunca se registran credenciales en logs. Nunca se devuelve la clave
completa al frontend.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_AI_ENCRYPTION_KEY_ENV = "AI_ENCRYPTION_KEY"
_AI_ENCRYPTION_KEY_PREVIOUS_ENV = "AI_ENCRYPTION_KEY_PREVIOUS"
_MIN_KEY_LEN = 32  # caracteres mínimos para AI_ENCRYPTION_KEY
_NONCE_SIZE = 12  # 96 bits, estándar para GCM
_HKDF_INFO = b"turnobot-ai-encryption-v1"
_CURRENT_KEY_VERSION = 1
_PREVIOUS_KEY_VERSION = 0


class AIEncryptionError(Exception):
    """Error de cifrado/descifrado de credenciales de IA."""


def _derive_key(raw_key: str) -> bytes:
    """Deriva una clave de 32 bytes desde una variable de entorno usando HKDF-SHA256."""
    if len(raw_key) < _MIN_KEY_LEN:
        raise AIEncryptionError(f"Clave de cifrado debe tener al menos {_MIN_KEY_LEN} caracteres")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO)
    return hkdf.derive(raw_key.encode("utf-8"))


def _get_aesgcm_for_env(env_name: str) -> AESGCM:
    """Obtiene una instancia AESGCM con la clave derivada de la variable dada.

    Raises AIEncryptionError si la variable no está definida o es invalida.
    """
    raw = os.getenv(env_name)
    if not raw:
        raise AIEncryptionError(f"{env_name} no está definida")
    return AESGCM(_derive_key(raw))


def _get_current_aesgcm() -> AESGCM:
    """Obtiene la instancia AESGCM de la clave vigente."""
    return _get_aesgcm_for_env(_AI_ENCRYPTION_KEY_ENV)


def _get_previous_aesgcm() -> AESGCM | None:
    """Obtiene la instancia AESGCM de la clave anterior, o None si no está configurada."""
    try:
        return _get_aesgcm_for_env(_AI_ENCRYPTION_KEY_PREVIOUS_ENV)
    except AIEncryptionError:
        return None


def _get_aesgcm_for_version(key_version: int) -> AESGCM:
    """Devuelve la AESGCM correspondiente a la versión de clave indicada."""
    if key_version == _CURRENT_KEY_VERSION:
        return _get_current_aesgcm()
    if key_version == _PREVIOUS_KEY_VERSION:
        previous = _get_previous_aesgcm()
        if previous is None:
            raise AIEncryptionError("Clave anterior no configurada")
        return previous
    raise AIEncryptionError(f"Version de clave desconocida: {key_version}")


def _decrypt_bytes(aesgcm: AESGCM, nonce: bytes, ciphertext: bytes) -> str | None:
    """Descifra con una clave concreta; None si no autentica o no es UTF-8."""
    try:
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError):
        return None


def _candidate_keys(key_version: int | None, errors: list[str]) -> list[tuple[str, AESGCM]]:
    """Construye la lista ordenada de claves a intentar para un layout dado.

    Para tokens versionados se prueba primero la clave indicada y después las
    restantes disponibles, lo que permite descifrar credenciales cifradas con
    la clave vigente anterior durante una rotación.
    """
    candidates: list[tuple[str, AESGCM]] = []
    seen: set[int] = set()

    if key_version is not None:
        try:
            candidates.append((f"clave_v{key_version}", _get_aesgcm_for_version(key_version)))
            seen.add(key_version)
        except AIEncryptionError as err:
            errors.append(str(err))

    if _CURRENT_KEY_VERSION not in seen:
        try:
            candidates.append(("clave_v1", _get_current_aesgcm()))
            seen.add(_CURRENT_KEY_VERSION)
        except AIEncryptionError as err:
            errors.append(str(err))

    if _PREVIOUS_KEY_VERSION not in seen:
        previous = _get_previous_aesgcm()
        if previous is not None:
            candidates.append(("clave_v0", previous))

    return candidates


def encrypt(plaintext: str, key_version: int = _CURRENT_KEY_VERSION) -> str:
    """Cifra con la clave de la versión indicada y etiqueta el payload.

    Por defecto usa la clave vigente. Si se solicita la versión anterior,
    se cifra realmente con `AI_ENCRYPTION_KEY_PREVIOUS`.
    """
    aesgcm = _get_aesgcm_for_version(key_version)
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = bytes([key_version]) + nonce + ciphertext
    return base64.b64encode(payload).decode("ascii")


def decrypt(token: str) -> str:
    """Descifra tokens versionados y legacy (nonce + ciphertext sin byte de versión)."""
    try:
        data = base64.b64decode(token, validate=True)
    except ValueError:
        raise AIEncryptionError("Token corrupto o con formato invalido") from None

    errors: list[str] = []
    min_versioned = 1 + _NONCE_SIZE + 16
    min_legacy = _NONCE_SIZE + 16

    def attempt(nonce: bytes, ciphertext: bytes, key_version: int | None) -> str | None:
        for label, aesgcm in _candidate_keys(key_version, errors):
            plaintext = _decrypt_bytes(aesgcm, nonce, ciphertext)
            if plaintext is not None:
                return plaintext
            errors.append(label)
        return None

    if len(data) >= min_versioned and data[0] in (_CURRENT_KEY_VERSION, _PREVIOUS_KEY_VERSION):
        plaintext = attempt(data[1 : 1 + _NONCE_SIZE], data[1 + _NONCE_SIZE :], data[0])
        if plaintext is not None:
            return plaintext
    elif len(data) >= min_versioned:
        errors.append(f"Version de clave desconocida: {data[0]}")

    if len(data) >= min_legacy:
        plaintext = attempt(data[:_NONCE_SIZE], data[_NONCE_SIZE:], None)
        if plaintext is not None:
            return plaintext

    detail = "; ".join(dict.fromkeys(errors)) if errors else "ninguna clave disponible"
    raise AIEncryptionError(f"No se pudo autenticar el token ({detail})") from None


def current_key_version() -> int:
    """Devuelve la versión de clave vigente para nuevas cifraciones."""
    return _CURRENT_KEY_VERSION


def is_configured() -> bool:
    """True si la clave vigente está definida y cumple el mínimo."""
    raw = os.getenv(_AI_ENCRYPTION_KEY_ENV)
    return bool(raw and len(raw) >= _MIN_KEY_LEN)


def previous_is_configured() -> bool:
    """True si la clave anterior está definida y cumple el mínimo."""
    raw = os.getenv(_AI_ENCRYPTION_KEY_PREVIOUS_ENV)
    return bool(raw and len(raw) >= _MIN_KEY_LEN)
