"""
tests/test_ai_encryption.py — Pruebas del servicio de cifrado AI_ENCRYPTION_KEY.

No requiere base de datos. Usa monkeypatch para controlar la variable de
entorno AI_ENCRYPTION_KEY.
"""

import pytest

from services.ai_encryption import AIEncryptionError, decrypt, encrypt, is_configured

pytestmark = pytest.mark.no_pg


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    plaintext = "sk-ant-api03-TestKey1234567890"
    token = encrypt(plaintext)
    assert token != plaintext
    assert decrypt(token) == plaintext


def test_different_nonce_produces_different_ciphertext(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    plaintext = "same-plaintext"
    t1 = encrypt(plaintext)
    t2 = encrypt(plaintext)
    assert t1 != t2  # nonces son aleatorios
    assert decrypt(t1) == plaintext
    assert decrypt(t2) == plaintext


def test_encryption_without_key_raises(monkeypatch):
    monkeypatch.delenv("AI_ENCRYPTION_KEY", raising=False)
    with pytest.raises(AIEncryptionError, match="no está definida"):
        encrypt("cualquier cosa")


def test_decrypt_without_key_raises(monkeypatch):
    monkeypatch.delenv("AI_ENCRYPTION_KEY", raising=False)
    # Token válido en formato pero sin clave configurada
    import base64

    token = base64.b64encode(bytes([1]) + b"\x00" * 12 + b"\x00" * 16).decode("ascii")
    with pytest.raises(AIEncryptionError, match="no est"):
        decrypt(token)


def test_is_configured_false_without_key(monkeypatch):
    monkeypatch.delenv("AI_ENCRYPTION_KEY", raising=False)
    assert is_configured() is False


def test_is_configured_true_with_key(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    assert is_configured() is True


def test_key_too_short_raises(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "short")
    with pytest.raises(AIEncryptionError, match="al menos 32 caracteres"):
        encrypt("texto")


def test_decrypt_corrupted_token_raises(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    token = encrypt("texto original")
    # Corromper el base64
    corrupted = token[:-2] + "!!"
    with pytest.raises(AIEncryptionError):
        decrypt(corrupted)


def test_different_keys_cannot_decrypt(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    token = encrypt("secreto")
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "b" * 32)
    with pytest.raises(AIEncryptionError):
        decrypt(token)


def test_decrypt_with_previous_key(monkeypatch):
    """Una credencial cifrada con la clave anterior puede descifrarse durante la transición."""
    import base64

    monkeypatch.setenv("AI_ENCRYPTION_KEY_PREVIOUS", "a" * 32)
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "b" * 32)
    plaintext = "sk-test-key-rotated"
    token = encrypt(plaintext, key_version=0)
    assert base64.b64decode(token)[0] == 0
    assert decrypt(token) == plaintext


def test_current_key_version_is_one(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    from services.ai_encryption import current_key_version

    assert current_key_version() == 1


def test_encrypt_tags_with_current_version(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    token = encrypt("texto")
    import base64

    data = base64.b64decode(token)
    assert data[0] == 1  # versión actual


def test_previous_key_not_configured_raises(monkeypatch):
    monkeypatch.setenv(
        "AI_ENCRYPTION_KEY", "z" * 32
    )  # clave actual distinta a la del cifrado manual
    # No hay AI_ENCRYPTION_KEY_PREVIOUS definida
    import base64
    import os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    # Cifrar manualmente con clave "anterior" simulada (distinta a la actual)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"turnobot-ai-encryption-v1")
    old_key = hkdf.derive(("a" * 32).encode("utf-8"))
    aesgcm_old = AESGCM(old_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm_old.encrypt(nonce, b"texto", None)
    token = base64.b64encode(bytes([0]) + nonce + ciphertext).decode("ascii")

    with pytest.raises(AIEncryptionError, match="Clave anterior no configurada"):
        decrypt(token)


def test_is_configured_previous_false_without_key(monkeypatch):
    monkeypatch.delenv("AI_ENCRYPTION_KEY_PREVIOUS", raising=False)
    from services.ai_encryption import previous_is_configured

    assert previous_is_configured() is False


def test_is_configured_previous_true_with_key(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY_PREVIOUS", "a" * 32)
    from services.ai_encryption import previous_is_configured

    assert previous_is_configured() is True


def test_encrypt_decrypt_with_previous_key_env(monkeypatch):
    """Rotación real: cifrar con la clave antigua y descifrar tras cambiar la vigente."""
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    plaintext = "sk-test-key-rotated-for-real"
    token = encrypt(plaintext)

    monkeypatch.setenv("AI_ENCRYPTION_KEY_PREVIOUS", "a" * 32)
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "b" * 32)
    assert decrypt(token) == plaintext


def test_decrypt_legacy_token_without_version_byte(monkeypatch):
    """Tokens anteriores al versionado siguen siendo legibles."""
    import base64
    import os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"turnobot-ai-encryption-v1")
    aesgcm = AESGCM(hkdf.derive(("a" * 32).encode("utf-8")))
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, b"legacy-secret", None)
    token = base64.b64encode(nonce + ciphertext).decode("ascii")

    assert decrypt(token) == "legacy-secret"


def test_encrypt_with_unknown_version_raises(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "a" * 32)
    with pytest.raises(AIEncryptionError, match="Version de clave desconocida"):
        encrypt("texto", key_version=9)
