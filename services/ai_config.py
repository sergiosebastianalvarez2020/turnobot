"""
services/ai_config.py — Configuración global de proveedores de IA (Super Admin).

CRUD de la tabla `platform_ai_providers` (configuración de plataforma, no
tenant-scoped). Las API Keys se almacenan cifradas con AES-256-GCM mediante
`services.ai_encryption`. Nunca se devuelve la clave descifrada a rutas,
plantillas ni respuestas JSON.

Todas las funciones escriben/leen a través de `database.database.get_connection()`
y registran acciones en `audit_log` mediante `log_platform_action`.
"""

from __future__ import annotations

import logging
from typing import Any

from database.database import get_connection, log_platform_action
from services.ai_encryption import AIEncryptionError, current_key_version, decrypt, encrypt
from services.ai_encryption import is_configured as encryption_is_configured

logger = logging.getLogger("turnobot.ai_config")

_VALID_PROVIDERS = frozenset({"gemini", "groq", "openrouter", "cerebras"})


class AIConfigError(Exception):
    """Error de configuración de proveedores de IA."""


def _require_encryption() -> None:
    """Verifica que AI_ENCRYPTION_KEY esté configurada antes de guardar credenciales."""
    if not encryption_is_configured():
        raise AIConfigError(
            "AI_ENCRYPTION_KEY no está configurada. No se pueden guardar credenciales de proveedores de IA."
        )


def _validate_api_key(api_key: str) -> str:
    """Valida que la credencial recibida no esté vacía."""
    value = (api_key or "").strip()
    if not value:
        raise AIConfigError("La API key no puede estar vacía")
    return value


def _validate_model(model: str) -> str:
    """Valida que el modelo configurado no esté vacío."""
    value = (model or "").strip()
    if not value:
        raise AIConfigError("El modelo no puede estar vacío")
    if len(value) > 128:
        raise AIConfigError("El modelo es demasiado largo")
    return value


def _validate_priority(priority: int) -> int:
    """Valida el rango de prioridad (999 queda reservado como valor por defecto)."""
    try:
        value = int(priority)
    except (TypeError, ValueError):
        raise AIConfigError("La prioridad debe ser un número entero") from None
    if value < 1 or value > 998:
        raise AIConfigError("La prioridad debe estar entre 1 y 998")
    return value


def _invalidate_router_cache() -> None:
    """Descarta la caché del router tras una mutación de configuración."""
    try:
        from services.ai_router import router
    except Exception:  # pragma: no cover - el router puede no estar inicializado
        return
    router.clear_cache()


def _mask_key(has_credentials: bool) -> str:
    """Devuelve una representación segura para mostrar al frontend.

    Nunca devuelve ni parte de la clave real. Solo indica si existe una
    credencial configurada.
    """
    if has_credentials:
        return "Configurada"
    return "Sin credenciales"


def _row_to_dict(row) -> dict[str, Any]:
    """Convierte una fila de BD a dict sin exponer la clave cifrada completa."""
    return {
        "id": row["id"],
        "provider": row["provider"],
        "model": row["model"],
        "priority": row["priority"],
        "active": bool(row["active"]),
        "has_credentials": bool(row["api_key_encrypted"]),
        "masked_key": _mask_key(bool(row["api_key_encrypted"])),
        "encryption_key_version": row["encryption_key_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ------------------------------------------------------------------
# Lectura
# ------------------------------------------------------------------


def list_ai_providers() -> list[dict[str, Any]]:
    """Lista todos los proveedores de IA (sin exponer claves cifradas)."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, provider, api_key_encrypted, model, priority, active,
                   encryption_key_version, created_at, updated_at
            FROM platform_ai_providers
            ORDER BY priority ASC, provider ASC
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        connection.close()


def get_ai_provider(provider_id: int) -> dict[str, Any] | None:
    """Obtiene un proveedor por id (sin exponer la clave cifrada)."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id, provider, api_key_encrypted, model, priority, active,
                   encryption_key_version, created_at, updated_at
            FROM platform_ai_providers
            WHERE id = ?
            """,
            (provider_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        connection.close()


def get_ai_provider_by_name(provider: str) -> dict[str, Any] | None:
    """Obtiene un proveedor por nombre (sin exponer la clave cifrada)."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id, provider, api_key_encrypted, model, priority, active,
                   encryption_key_version, created_at, updated_at
            FROM platform_ai_providers
            WHERE provider = ?
            """,
            (provider,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        connection.close()


def get_decrypted_api_key(provider_id: int) -> str | None:
    """
    Obtiene la API Key descifrada de un proveedor (USO INTERNO SOLO).

    Nunca se expone a rutas, plantillas ni respuestas JSON.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT api_key_encrypted FROM platform_ai_providers WHERE id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            return None
        encrypted = row["api_key_encrypted"]
        if not encrypted:
            return None
        try:
            return decrypt(encrypted)
        except AIEncryptionError:
            logger.warning("No se pudo descifrar la API Key del proveedor id=%s", provider_id)
            return None
    finally:
        connection.close()


# ------------------------------------------------------------------
# Escritura
# ------------------------------------------------------------------


def _priority_is_unique(connection, priority: int, exclude_id: int | None = None) -> bool:
    """Verifica que ninguna otra fila use la misma prioridad."""
    if exclude_id is not None:
        row = connection.execute(
            "SELECT 1 FROM platform_ai_providers WHERE priority = ? AND id != ?",
            (priority, exclude_id),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM platform_ai_providers WHERE priority = ?", (priority,)
        ).fetchone()
    return row is None


def create_ai_provider(
    provider: str,
    api_key: str,
    model: str,
    priority: int,
    active: bool,
    platform_user_id: int | None = None,
    actor_email: str = "",
    ip_address: str = "",
) -> int:
    """
    Crea un nuevo proveedor de IA con credencial cifrada.

    Devuelve el id del proveedor creado.
    """
    if provider not in _VALID_PROVIDERS:
        raise AIConfigError(f"Proveedor no válido: {provider}")
    api_key = _validate_api_key(api_key)
    model = _validate_model(model)
    priority = _validate_priority(priority)
    _require_encryption()

    if get_ai_provider_by_name(provider) is not None:
        raise AIConfigError(f"El proveedor '{provider}' ya está configurado")

    connection = get_connection()
    try:
        if not _priority_is_unique(connection, priority):
            raise AIConfigError(f"La prioridad {priority} ya está en uso")

        encrypted_key = encrypt(api_key)
        key_version = current_key_version()

        cursor = connection.execute(
            """
            INSERT INTO platform_ai_providers
            (provider, api_key_encrypted, model, priority, active, encryption_key_version)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (provider, encrypted_key, model, int(priority), 1 if active else 0, key_version),
        )
        connection.commit()
        provider_id = cursor.lastrowid
        _invalidate_router_cache()

        log_platform_action(
            platform_user_id=platform_user_id,
            actor_email=actor_email,
            business_id=None,
            action="ai_provider_create",
            detail=f"provider={provider}",
            ip_address=ip_address,
        )
        return provider_id
    finally:
        connection.close()


def update_ai_provider(
    provider_id: int,
    model: str | None = None,
    priority: int | None = None,
    active: bool | None = None,
    api_key: str | None = None,
    platform_user_id: int | None = None,
    actor_email: str = "",
    ip_address: str = "",
) -> bool:
    """
    Actualiza un proveedor existente.

    Si `api_key` no se proporciona, se conserva la credencial existente.
    Devuelve True si se actualizó al menos un campo.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT provider, api_key_encrypted, priority FROM platform_ai_providers WHERE id = ?",
            (provider_id,),
        ).fetchone()
        if row is None:
            return False

        provider_name = row["provider"]
        current_priority = row["priority"]

        fields: list[str] = []
        params: list[Any] = []

        if model is not None:
            model = _validate_model(model)
            fields.append("model = ?")
            params.append(model)
        if priority is not None:
            new_priority = _validate_priority(priority)
            if new_priority != current_priority and not _priority_is_unique(
                connection, new_priority, exclude_id=provider_id
            ):
                raise AIConfigError(f"La prioridad {new_priority} ya está en uso")
            fields.append("priority = ?")
            params.append(new_priority)
        if active is not None:
            fields.append("active = ?")
            params.append(1 if active else 0)
        if api_key is not None:
            api_key = _validate_api_key(api_key)
            _require_encryption()
            fields.append("api_key_encrypted = ?")
            params.append(encrypt(api_key))
            fields.append("encryption_key_version = ?")
            params.append(current_key_version())

        if not fields:
            return False

        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(provider_id)

        connection.execute(
            f"UPDATE platform_ai_providers SET {', '.join(fields)} WHERE id = ?", params
        )
        connection.commit()
        _invalidate_router_cache()

        detail_parts = [f"provider={provider_name}"]
        if model is not None:
            detail_parts.append(f"model={model}")
        if priority is not None:
            detail_parts.append(f"priority={priority}")
        if active is not None:
            detail_parts.append(f"active={active}")
        if api_key is not None:
            detail_parts.append("credencial_rotada")
        log_platform_action(
            platform_user_id=platform_user_id,
            actor_email=actor_email,
            business_id=None,
            action="ai_provider_update",
            detail=", ".join(detail_parts),
            ip_address=ip_address,
        )
        return True
    finally:
        connection.close()


def set_ai_provider_active(
    provider_id: int,
    active: bool,
    platform_user_id: int | None = None,
    actor_email: str = "",
    ip_address: str = "",
) -> bool:
    """Activa o desactiva un proveedor. Devuelve True si existió."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT provider FROM platform_ai_providers WHERE id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            return False

        provider_name = row["provider"]
        connection.execute(
            "UPDATE platform_ai_providers SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (1 if active else 0, provider_id),
        )
        connection.commit()
        _invalidate_router_cache()

        action = "ai_provider_activate" if active else "ai_provider_deactivate"
        log_platform_action(
            platform_user_id=platform_user_id,
            actor_email=actor_email,
            business_id=None,
            action=action,
            detail=f"provider={provider_name}",
            ip_address=ip_address,
        )
        return True
    finally:
        connection.close()


def set_ai_provider_priority(
    provider_id: int,
    priority: int,
    platform_user_id: int | None = None,
    actor_email: str = "",
    ip_address: str = "",
) -> bool:
    """Cambia la prioridad de un proveedor. Devuelve True si existió."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT provider, priority FROM platform_ai_providers WHERE id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            return False

        provider_name = row["provider"]
        priority = _validate_priority(priority)
        current_priority = row["priority"]
        if priority == current_priority:
            return True

        if not _priority_is_unique(connection, priority, exclude_id=provider_id):
            raise AIConfigError(f"La prioridad {priority} ya está en uso")

        connection.execute(
            "UPDATE platform_ai_providers SET priority = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (priority, provider_id),
        )
        connection.commit()
        _invalidate_router_cache()

        log_platform_action(
            platform_user_id=platform_user_id,
            actor_email=actor_email,
            business_id=None,
            action="ai_provider_priority",
            detail=f"provider={provider_name}, priority={priority}",
            ip_address=ip_address,
        )
        return True
    finally:
        connection.close()


def delete_ai_provider(
    provider_id: int,
    platform_user_id: int | None = None,
    actor_email: str = "",
    ip_address: str = "",
) -> bool:
    """Elimina un proveedor. Devuelve True si existió."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT provider FROM platform_ai_providers WHERE id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            return False

        provider_name = row["provider"]
        connection.execute("DELETE FROM platform_ai_providers WHERE id = ?", (provider_id,))
        connection.commit()
        _invalidate_router_cache()

        log_platform_action(
            platform_user_id=platform_user_id,
            actor_email=actor_email,
            business_id=None,
            action="ai_provider_delete",
            detail=f"provider={provider_name}",
            ip_address=ip_address,
        )
        return True
    finally:
        connection.close()
