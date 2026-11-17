"""Capa de negocio de la identidad visual multi-tenant (Fase 2).

Responsabilidades:
- Cargar/guardar la config visual por tenant (`business_visual_identity`).
- Validar (colores HEX, fonts allowlist, media, opacidad) y normalizar.
- Verificar contraste WCAG ≥ 4.5:1 (texto/fondo y botón/label de botón).
- Espejar primary/secondary/logo hacia `business_settings` para los lectores
  legacy (login/admin/emails) sin tocar el resto de su config.
- Aplicar las opciones bloqueadas por el Super Admin (`locked_options`).

Patrón de respuesta: dict `{"success": bool, "reason": str|None, ...}` como
services/memberships.py. Toda escritura está scoped por business_id.
"""

from __future__ import annotations

import json
import logging

from application.theme_colors import contrast_ratio, is_valid_hex
from application.themes import (
    COLOR_KEYS,
    CONFIG_KEYS,
    LOCKABLE_OPTIONS,
    MEDIA_KEYS,
    default_config,
    normalize_config,
)
from database.database import (
    acquire_business_write_lock,
    get_backend,
    get_connection,
    get_visual_identity_scoped,
    set_visual_identity_locks_scoped,
)

logger = logging.getLogger("turnobot.visual_identity")

# Advisory lock por negocio para mutaciones de identidad visual (int4).
_VISUAL_IDENTITY_LOCK_KEY = 3_000_000_001

# Contraste mínimo WCAG 2.1 AA para texto normal.
MIN_CONTRAST_RATIO = 4.5


def _denied(reason, **extra):
    payload = {"success": False, "reason": reason}
    payload.update(extra)
    return payload


def _serialize(config: dict, locked: list[str]) -> tuple[str, str]:
    return (
        json.dumps(config, ensure_ascii=False, sort_keys=True),
        json.dumps(sorted(set(locked)), ensure_ascii=False),
    )


def _parse_json(raw, fallback):
    if raw in (None, ""):
        return fallback
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, (dict, list)) else fallback


def _mirror_to_business_settings(business_id, config):
    """Espeja primary/secondary/logo a business_settings (lectores legacy)."""
    connection = get_connection()
    try:
        connection.execute(
            """
            UPDATE business_settings
            SET primary_color = ?,
                secondary_color = ?,
                logo_url = ?
            WHERE business_id = ?
            """,
            (
                config.get("primary_color", ""),
                config.get("secondary_color", ""),
                config.get("logo_url", ""),
                business_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def validate_config(config, *, base=None):
    """Valida y normaliza una config visual.

    Returns:
        (normalized_config, errors) — errors vacío si la config es válida.
    """
    raw = dict(config or {})
    errors: list[str] = []

    # 1) Colores: presentes y HEX válidos si vienen definidos.
    for key in COLOR_KEYS:
        value = raw.get(key)
        if value in (None, ""):
            continue
        if not is_valid_hex(str(value)):
            errors.append(f"{key}: debe ser un HEX #RRGGBB válido")

    # 2) Fonts allowlist.
    font = raw.get("font_family")
    if font not in (None, ""):
        from application.themes import is_allowed_font

        if not is_allowed_font(str(font)):
            errors.append("font_family: tipografía no permitida")

    # 3) Media: solo rutas locales (/media/...) o http(s) https.
    for key in MEDIA_KEYS:
        value = str(raw.get(key) or "").strip()
        if not value:
            continue
        if value.startswith("/media/"):
            continue
        if value.startswith(("http://", "https://")):
            # Proxy de imágenes externas heredadas sigue siendo https.
            continue
        errors.append(f"{key}: debe ser una ruta /media/... o una URL http(s)")

    # 4) Opacidad numérica en [0, 1].
    opacity_raw = raw.get("header_overlay_opacity")
    if opacity_raw not in (None, ""):
        try:
            opacity = float(opacity_raw)
            if not 0.0 <= opacity <= 1.0:
                errors.append("header_overlay_opacity: debe estar entre 0 y 1")
        except (TypeError, ValueError):
            errors.append("header_overlay_opacity: debe ser un número entre 0 y 1")

    if errors:
        return None, errors

    normalized = normalize_config(raw, base=base)
    return normalized, []


def check_contrast(config):
    """Verifica contraste WCAG ≥ 4.5:1 (texto/fondo y botón/label de botón).

    Returns:
        list[str] con las violaciones (vacía si todo cumple).
    """
    violations: list[str] = []
    pairs = (
        ("page_text_color", "page_background_color", "texto sobre fondo"),
        ("button_text_color", "button_bg_color", "label sobre botón"),
    )
    for fg_key, bg_key, label in pairs:
        fg = config.get(fg_key)
        bg = config.get(bg_key)
        if not (fg and bg):
            continue
        ratio = contrast_ratio(fg, bg)
        if ratio < MIN_CONTRAST_RATIO:
            violations.append(
                f"{label}: contraste {ratio:.2f}:1 < {MIN_CONTRAST_RATIO}:1 ({fg} sobre {bg})"
            )
    return violations


def load_identity(business_id, settings_row=None):
    """Carga la config visual efectiva de un negocio.

    Si hay fila en business_visual_identity, la config guardada es autoridad y
    el branding legacy solo completa huecos (p. ej. logo).

    Si NO hay fila, el branding legacy de `business_settings` manda para los
    colores/logo (compatibilidad con tenants pre-migración); el preset solo
    aporta el resto de las claves.
    """
    if business_id is None:
        return default_config(), []

    row = get_visual_identity_scoped(business_id)
    locked = _parse_json(row["locked_options"], []) if row else []
    if not isinstance(locked, list):
        locked = []

    base = default_config()

    # Legacy: la fila de settings pasada (o una consulta directa si no vino).
    if settings_row is None:
        connection = get_connection()
        try:
            legacy = connection.execute(
                """
                SELECT logo_url, primary_color, secondary_color
                FROM business_settings
                WHERE business_id = ?
                """,
                (business_id,),
            ).fetchone()
        finally:
            connection.close()
    else:
        legacy = settings_row
    legacy = dict(legacy) if legacy is not None else None

    if row:
        stored = _parse_json(row["config"], {})
        if isinstance(stored, dict) and stored:
            base.update({k: v for k, v in stored.items() if k in CONFIG_KEYS})
        # La identidad guardada es autoridad; el legacy solo completa huecos.
        if legacy:
            if not base.get("logo_url") and legacy.get("logo_url"):
                base["logo_url"] = legacy["logo_url"]
            if not base.get("primary_color") and legacy.get("primary_color"):
                base["primary_color"] = legacy["primary_color"]
            if not base.get("secondary_color") and legacy.get("secondary_color"):
                base["secondary_color"] = legacy["secondary_color"]
    elif legacy:
        # Sin identidad guardada: el branding legacy manda (pre-migración).
        if legacy.get("primary_color"):
            base["primary_color"] = legacy["primary_color"]
        if legacy.get("secondary_color"):
            base["secondary_color"] = legacy["secondary_color"]
        if legacy.get("logo_url"):
            base["logo_url"] = legacy["logo_url"]

    normalized = normalize_config(base, base=default_config())
    return normalized, locked


def get_locked_options(business_id):
    """Devuelve la lista de opciones bloqueadas por el Super Admin."""
    row = get_visual_identity_scoped(business_id)
    if not row:
        return []
    locked = _parse_json(row["locked_options"], [])
    return locked if isinstance(locked, list) else []


def save_identity(business_id, config, *, actor=None, enforce_locks=True):
    """Guarda la config visual de un negocio (upsert transaccional).

    Valida, verifica contraste, aplica locks del Super Admin, persiste y
    espeja a business_settings.
    """
    if business_id is None:
        return _denied("invalid_business")
    try:
        business_id = int(business_id)
    except (TypeError, ValueError):
        return _denied("invalid_business")

    current, locked = load_identity(business_id)

    # Campos bloqueados: conservar el valor vigente del tenant.
    if enforce_locks and locked:
        config = dict(config or {})
        for key in locked:
            if key in CONFIG_KEYS and key in current:
                config[key] = current[key]

    normalized, errors = validate_config(config, base=current)
    if errors:
        return _denied("invalid_config", errors=errors)

    contrast_violations = check_contrast(normalized)
    if contrast_violations:
        return _denied("contrast_below_minimum", violations=contrast_violations)

    connection = get_connection()
    try:
        if get_backend() != "postgresql":
            connection.execute("BEGIN IMMEDIATE")
        acquire_business_write_lock(connection, business_id, _VISUAL_IDENTITY_LOCK_KEY)

        config_json, locked_json = _serialize(normalized, locked)
        connection.execute(
            """
            INSERT INTO business_visual_identity (business_id, config, locked_options)
            VALUES (?, ?, ?)
            ON CONFLICT (business_id) DO UPDATE SET
                config = excluded.config,
                updated_at = CURRENT_TIMESTAMP
            """,
            (business_id, config_json, locked_json),
        )
        connection.execute(
            """
            UPDATE business_settings
            SET primary_color = ?,
                secondary_color = ?,
                logo_url = ?
            WHERE business_id = ?
            """,
            (
                normalized.get("primary_color", ""),
                normalized.get("secondary_color", ""),
                normalized.get("logo_url", ""),
                business_id,
            ),
        )
        connection.commit()
    except Exception as exc:  # noqa: BLE001 — se traduce a razón de negocio
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.error("save_identity falló (business=%s): %s", business_id, exc)
        return _denied("storage_error")
    finally:
        connection.close()

    return {"success": True, "config": normalized, "actor": actor}


def save_locked_options(business_id, locked_options):
    """Define las opciones bloqueadas (solo Super Admin)."""
    if business_id is None:
        return _denied("invalid_business")

    requested = locked_options if isinstance(locked_options, (list, tuple)) else []
    unknown = [str(k) for k in requested if str(k) not in LOCKABLE_OPTIONS]
    if unknown:
        return _denied("unknown_options", options=unknown)

    try:
        set_visual_identity_locks_scoped(business_id, json.dumps(sorted(set(requested))))
    except Exception as exc:  # noqa: BLE001
        logger.error("save_locked_options falló (business=%s): %s", business_id, exc)
        return _denied("storage_error")
    return {"success": True, "locked_options": sorted(set(requested))}


def apply_preset(business_id, preset_name, *, actor=None, enforce_locks=True):
    """Aplica un preset completo (con overrides de locks) y lo persiste."""
    from application.themes import get_preset

    config = get_preset(preset_name)
    if config.get("theme_preset") != str(preset_name or "").strip().lower():
        return _denied("unknown_preset")

    current, locked = load_identity(business_id)
    # Preset reemplaza la paleta pero conserva media (logo/covers) ya cargada.
    for key in MEDIA_KEYS:
        config[key] = current.get(key, "")
    if enforce_locks and locked:
        for key in locked:
            if key in CONFIG_KEYS and key in current:
                config[key] = current[key]
    return save_identity(business_id, config, actor=actor, enforce_locks=False)


def effective_public_settings(business_id, settings_row):
    """Combina settings legacy + identidad visual para las páginas públicas.

    Devuelve un dict con las claves que build_public_frontend_config espera
    (business_* + branding/media), priorizando la identidad visual. Si no hay
    identidad guardada, el branding legacy del settings_row manda.
    """
    merged = dict(settings_row) if settings_row else {}
    config, _locked = load_identity(business_id, settings_row=settings_row)
    merged["primary_color"] = config.get("primary_color", "")
    merged["secondary_color"] = config.get("secondary_color", "")
    merged["logo_url"] = config.get("logo_url", "")
    merged["page_background_color"] = config.get("page_background_color", "")
    merged["page_text_color"] = config.get("page_text_color", "")
    merged["font_family"] = config.get("font_family", "")
    merged["visual_identity"] = config
    return merged
