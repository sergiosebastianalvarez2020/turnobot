"""Derivación de paleta de colores para identidad visual de páginas públicas.

Convierte un color primario/secundario HEX en el set completo de variables CSS
que la cascada de style.css necesita (hover, dark, light, rgb triplet, etc.).
Sin dependencias externas: solo aritmética RGB.
"""

from __future__ import annotations

import re

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_PRIMARY = "#1463FF"
DEFAULT_SECONDARY = "#0B1B3A"
DEFAULT_BACKGROUND = "#EAF4FF"
DEFAULT_TEXT = "#102A56"
DEFAULT_SURFACE = "#FFFFFF"
DEFAULT_BORDER = "#C9DDF8"
DEFAULT_TEXT_SECONDARY = "#4A6590"
DEFAULT_FONT = "DM Sans"


def is_valid_hex(value) -> bool:
    """True si value es un HEX #RRGGBB válido."""
    return isinstance(value, str) and bool(_HEX_RE.match(value.strip()))


def normalize_hex(value, fallback: str) -> str:
    """Devuelve un HEX válido; fallback si el valor es inválido o vacío."""
    if value is None:
        return fallback
    candidate = str(value).strip()
    if is_valid_hex(candidate):
        return candidate.upper()
    return fallback


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """#RRGGBB → (r, g, b). Lanza ValueError si no es HEX válido."""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"HEX inválido: {hex_color!r}")
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def rgb_to_hex(r: float, g: float, b: float) -> str:
    """(r, g, b) → #RRGGBB con canales recortados a [0, 255]."""
    clamp = lambda c: max(0, min(255, int(round(c))))  # noqa: E731
    return f"#{clamp(r):02X}{clamp(g):02X}{clamp(b):02X}"


def rgb_triplet(hex_color: str) -> str:
    """#RRGGBB → 'r, g, b' para rgba(var(--x-rgb), alpha)."""
    r, g, b = hex_to_rgb(hex_color)
    return f"{r}, {g}, {b}"


def _mix(hex_color: str, target: tuple[int, int, int], amount: float) -> str:
    """Mezcla hex_color hacia target en [0, 1]."""
    r, g, b = hex_to_rgb(hex_color)
    tr, tg, tb = target
    return rgb_to_hex(r + (tr - r) * amount, g + (tg - g) * amount, b + (tb - b) * amount)


def darken(hex_color: str, amount: float) -> str:
    """Oscurece mezclando hacia negro."""
    return _mix(hex_color, (0, 0, 0), amount)


def lighten(hex_color: str, amount: float) -> str:
    """Aclara mezclando hacia blanco."""
    return _mix(hex_color, (255, 255, 255), amount)


def relative_luminance(hex_color: str) -> float:
    """Luminancia relativa WCAG 2.1 (0..1)."""

    def _channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(hex_color)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """Ratio de contraste WCAG entre dos colores (1..21)."""
    la = relative_luminance(hex_a)
    lb = relative_luminance(hex_b)
    lighter = max(la, lb)
    darker = min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def readable_text_color(background: str, *, light: str = "#FFFFFF", dark: str = "#111827") -> str:
    """Devuelve el color de texto con mayor contraste sobre `background`."""
    try:
        light_ratio = contrast_ratio(light, background)
        dark_ratio = contrast_ratio(dark, background)
    except ValueError:
        return dark
    return light if light_ratio >= dark_ratio else dark


def palette_summary(config: dict | None = None) -> dict:
    """Resumen visual de la paleta efectiva (incluye superficie derivada).

    No agrega claves persistidas: la superficie sigue siendo el token
    `DEFAULT_SURFACE` que usa la cascada pública.
    """
    config = dict(config or {})
    palette = derive_palette(
        config.get("primary_color"),
        config.get("secondary_color"),
        config.get("page_background_color"),
        config.get("page_text_color"),
    )
    return {
        "primary": palette["primary"],
        "secondary": palette["secondary"],
        "background": palette["background"],
        "surface": palette["surface"],
        "text": palette["text"],
        "on_primary": readable_text_color(palette["primary"]),
        "on_secondary": readable_text_color(palette["secondary"]),
    }


def contrast_pairs(config: dict | None = None) -> list[dict]:
    """Pares WCAG que valida el guardado, con ratios listos para la UI."""
    config = dict(config or {})
    definitions = (
        ("text", "page_text_color", "page_background_color", "Texto sobre fondo"),
        ("button", "button_text_color", "button_bg_color", "Texto de botón sobre fondo"),
    )
    results: list[dict] = []
    for key, fg_key, bg_key, label in definitions:
        fg = normalize_hex(config.get(fg_key), DEFAULT_PRIMARY)
        bg = normalize_hex(config.get(bg_key), DEFAULT_BACKGROUND)
        try:
            ratio = contrast_ratio(fg, bg)
        except ValueError:
            continue
        results.append(
            {
                "key": key,
                "label": label,
                "foreground": fg,
                "background": bg,
                "ratio": ratio,
                "passes": ratio >= 4.5,
            }
        )
    return results


def derive_palette(
    primary: str | None = None,
    secondary: str | None = None,
    background: str | None = None,
    text: str | None = None,
) -> dict:
    """Deriva el set completo de tokens de color para las páginas públicas.

    Valores vacíos/inválidos caen a los defaults de la plataforma.
    Los derivados (hover/dark/light) se calculan desde primary cuando no se
    proveen explícitamente.
    """
    p = normalize_hex(primary, DEFAULT_PRIMARY)
    s = normalize_hex(secondary, DEFAULT_SECONDARY)
    bg = normalize_hex(background, DEFAULT_BACKGROUND)
    tx = normalize_hex(text, DEFAULT_TEXT)

    return {
        "primary": p,
        "primary_dark": darken(p, 0.18),
        "primary_hover": darken(p, 0.12),
        "primary_light": lighten(p, 0.22),
        "primary_deep": s,
        "primary_rgb": rgb_triplet(p),
        "secondary": s,
        "background": bg,
        "surface": DEFAULT_SURFACE,
        "surface_hover": "#F8FAFF",
        "text": tx,
        "text_secondary": DEFAULT_TEXT_SECONDARY,
        "text_muted": "#5B6B84",
        "border": DEFAULT_BORDER,
        "blue_border": DEFAULT_BORDER,
        "font_family": DEFAULT_FONT,
    }


def build_theme_css_variables(theme: dict) -> str:
    """Serializa un dict de theme a declaraciones CSS `--var: valor;`.

    Usado por los templates para inyectar el set completo en :root/body.
    """
    order = [
        "primary",
        "primary_dark",
        "primary_hover",
        "primary_light",
        "primary_deep",
        "primary_rgb",
        "secondary",
        "background",
        "surface",
        "surface_hover",
        "text",
        "text_secondary",
        "text_muted",
        "border",
        "blue_border",
        "font_family",
    ]
    parts: list[str] = []
    for key in order:
        if key not in theme or theme[key] in (None, ""):
            continue
        value = str(theme[key])
        if key == "font_family":
            parts.append(f"--font-family: '{value}'")
        else:
            parts.append(f"--{key.replace('_', '-')}: {value}")
    # Extras no estandar (p. ej. gradientes) se agregan al final.
    for key, value in theme.items():
        if key in order or value in (None, ""):
            continue
        parts.append(f"--{key.replace('_', '-')}: {value}")
    return "; ".join(parts)
