"""Biblioteca de temas (presets) para la identidad visual multi-tenant.

Cada preset es una baseline editable: el tenant parte del preset y luego puede
ajustar colores/tipografía. El preset NO se conserva como autoridad — se
guarda el nombre del preset aplicado y la config resultante (editada o no).

Sin dependencias externas: dicts planos serializables a JSON.
"""

from __future__ import annotations

from application.theme_colors import DEFAULT_FONT, normalize_hex

# Claves canónicas de configuración visual (JSON de business_visual_identity).
CONFIG_KEYS = (
    "theme_preset",
    "primary_color",
    "secondary_color",
    "page_background_color",
    "page_text_color",
    "button_bg_color",
    "button_text_color",
    "font_family",
    "logo_url",
    "favicon_url",
    "cover_image_url",
    "header_bg_image_url",
    "header_overlay_opacity",
)

# Campos de media (rutas o URLs) — para validación de uploads.
MEDIA_KEYS = (
    "logo_url",
    "favicon_url",
    "cover_image_url",
    "header_bg_image_url",
)

# Campos de color HEX.
COLOR_KEYS = (
    "primary_color",
    "secondary_color",
    "page_background_color",
    "page_text_color",
    "button_bg_color",
    "button_text_color",
)

# Font families permitidas (allowlist anti inyección CSS).
ALLOWED_FONTS = (
    "DM Sans",
    "Inter",
    "Poppins",
    "Roboto",
    "Montserrat",
    "Lato",
    "Source Sans 3",
    "Nunito",
    "Work Sans",
    "Playfair Display",
    "Merriweather",
    "system-ui",
)

# Opciones que el Super Admin puede bloquear por tenant (locked_options).
LOCKABLE_OPTIONS = CONFIG_KEYS

DEFAULT_HEADER_OVERLAY_OPACITY = 0.55


def _preset(
    name: str,
    *,
    primary: str,
    secondary: str,
    background: str,
    text: str,
    font: str = DEFAULT_FONT,
    button_bg: str | None = None,
    button_text: str = "#FFFFFF",
) -> dict:
    return {
        "theme_preset": name,
        "primary_color": primary,
        "secondary_color": secondary,
        "page_background_color": background,
        "page_text_color": text,
        "button_bg_color": button_bg or primary,
        "button_text_color": button_text,
        "font_family": font,
        "logo_url": "",
        "favicon_url": "",
        "cover_image_url": "",
        "header_bg_image_url": "",
        "header_overlay_opacity": DEFAULT_HEADER_OVERLAY_OPACITY,
    }


# 8 presets con contraste alto (texto sobre fondo ≥ 4.5:1 verificado en tests).
THEME_PRESETS: dict[str, dict] = {
    "minimalista": _preset(
        "minimalista",
        primary="#1F2937",
        secondary="#111827",
        background="#FFFFFF",
        text="#111827",
        font="Inter",
        button_text="#FFFFFF",
    ),
    "elegante": _preset(
        "elegante",
        primary="#7C2D12",
        secondary="#431407",
        background="#FFFBF5",
        text="#292524",
        font="Playfair Display",
        button_text="#FFFFFF",
    ),
    "moderno": _preset(
        "moderno",
        primary="#1463FF",
        secondary="#0B1B3A",
        background="#EAF4FF",
        text="#102A56",
        font="DM Sans",
        button_text="#FFFFFF",
    ),
    "corporativo": _preset(
        "corporativo",
        primary="#0F4C81",
        secondary="#082F49",
        background="#F4F7FB",
        text="#0F172A",
        font="Source Sans 3",
        button_text="#FFFFFF",
    ),
    "premium": _preset(
        "premium",
        primary="#4C1D95",
        secondary="#2E1065",
        background="#FAF5FF",
        text="#1E1B4B",
        font="Poppins",
        button_text="#FFFFFF",
    ),
    "urbano": _preset(
        "urbano",
        primary="#0F766E",
        secondary="#134E4A",
        background="#F0FDFA",
        text="#134E4A",
        font="Work Sans",
        button_text="#FFFFFF",
    ),
    "claro": _preset(
        "claro",
        primary="#2563EB",
        secondary="#1E40AF",
        background="#FFFFFF",
        text="#111827",
        font="Nunito",
        button_text="#FFFFFF",
    ),
    "oscuro": _preset(
        "oscuro",
        primary="#38BDF8",
        secondary="#0EA5E9",
        background="#0B1120",
        text="#E2E8F0",
        font="Inter",
        button_text="#082F49",
    ),
}

DEFAULT_PRESET = "moderno"


def get_preset(name: str | None) -> dict:
    """Devuelve una copia del preset; fallback a `moderno` si no existe."""
    preset = THEME_PRESETS.get((name or "").strip().lower())
    if preset is None:
        preset = THEME_PRESETS[DEFAULT_PRESET]
    return dict(preset)


def preset_names() -> list[str]:
    """Lista de nombres de presets en orden estable."""
    return list(THEME_PRESETS.keys())


def default_config() -> dict:
    """Config visual por defecto de un negocio nuevo (preset moderno)."""
    return get_preset(DEFAULT_PRESET)


def is_allowed_font(font_family: str | None) -> bool:
    return (font_family or "").strip() in ALLOWED_FONTS


def normalize_font(font_family: str | None, fallback: str = DEFAULT_FONT) -> str:
    candidate = (font_family or "").strip()
    return candidate if is_allowed_font(candidate) else fallback


def normalize_opacity(value, fallback: float = DEFAULT_HEADER_OVERLAY_OPACITY) -> float:
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, opacity))


def normalize_config(raw: dict | None, base: dict | None = None) -> dict:
    """Normaliza un config parcial a un config completo y seguro.

    - `base` (opcional) aporta los valores ausentes (p. ej. config previa o preset).
    - Colores inválidos caen a los valores de base o a los defaults del preset.
    - Claves desconocidas se descartan (allowlist CONFIG_KEYS).
    """
    raw = dict(raw or {})
    base = dict(base or default_config())
    out: dict = {}

    preset_name = str(raw.get("theme_preset") or base.get("theme_preset") or DEFAULT_PRESET)
    preset_name = preset_name.strip().lower()
    if preset_name not in THEME_PRESETS:
        preset_name = DEFAULT_PRESET
    out["theme_preset"] = preset_name

    for key in COLOR_KEYS:
        fallback = str(base.get(key) or THEME_PRESETS[preset_name][key])
        out[key] = normalize_hex(raw.get(key, base.get(key)), fallback)

    font = raw.get("font_family", base.get("font_family"))
    out["font_family"] = normalize_font(font, THEME_PRESETS[preset_name]["font_family"])

    out["header_overlay_opacity"] = normalize_opacity(
        raw.get("header_overlay_opacity", base.get("header_overlay_opacity"))
    )

    for key in MEDIA_KEYS:
        value = raw.get(key, base.get(key, ""))
        out[key] = str(value or "").strip()

    return out
