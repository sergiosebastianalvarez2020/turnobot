"""Contrato público del frontend y límites de chat (Bloque 5B, Paso 5).

build_public_frontend_config y las constantes de límites de mensajes/chat
extraídos de app.py, manteniendo lógica idéntica.
"""

from application.media import rewrite_external_logo_url
from application.theme_colors import (
    DEFAULT_BACKGROUND,
    DEFAULT_FONT,
    DEFAULT_PRIMARY,
    DEFAULT_SECONDARY,
    DEFAULT_TEXT,
    derive_palette,
    normalize_hex,
)

MAX_MESSAGE_LENGTH = 1_000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_LENGTH = 2_000


def build_public_frontend_config(settings=None):
    """Construye el contrato público del frontend sin exponer autoridad tenant."""
    settings = settings or {}

    def value(key, default):
        try:
            current = settings[key]
        except (KeyError, IndexError, TypeError):
            current = None
        return current or default

    def raw(key):
        try:
            current = settings[key]
        except (KeyError, IndexError, TypeError):
            current = None
        return current

    primary = normalize_hex(raw("primary_color"), DEFAULT_PRIMARY)
    secondary = normalize_hex(raw("secondary_color"), DEFAULT_SECONDARY)
    background = normalize_hex(raw("page_background_color"), DEFAULT_BACKGROUND)
    text = normalize_hex(raw("page_text_color"), DEFAULT_TEXT)
    font_family = str(value("font_family", DEFAULT_FONT)) or DEFAULT_FONT

    # Paleta completa: derivados (hover/dark/light/rgb) calculados desde primary
    # para que la cascada de style.css no quede pegada al azul por defecto.
    palette = derive_palette(
        primary=primary,
        secondary=secondary,
        background=background,
        text=text,
    )
    palette["font_family"] = font_family

    return {
        "business": {
            "name": value("business_name", "Mi negocio"),
            "type": value("business_type", "Negocio"),
            "initials": value("business_initials", ""),
            "description": value("business_description", ""),
            "timezone": value("timezone", "UTC"),
            "notifications_enabled": bool(value("notifications_enabled", 0)),
        },
        "branding": {
            "logo_url": rewrite_external_logo_url(value("logo_url", "")) or "",
            "primary_color": primary,
            "secondary_color": secondary,
        },
        "content": {
            "welcome_label": "BIENVENIDO A {business_name}",
            "welcome_title": "Tu próxima visita empieza acá.",
            "welcome_description": "Soy el recepcionista virtual de {business_name}, {business_description}. Puedo ayudarte con servicios, horarios y turnos.",
            "initial_message": "¡Hola! 👋\n\n¿En qué puedo ayudarte hoy?",
            "quick_actions": [
                {
                    "label": "Servicios",
                    "sub": "Ver todos los servicios",
                    "message": "¿Qué servicios tienen y cuánto cuestan?",
                },
                {
                    "label": "Disponibilidad",
                    "sub": "Ver horarios disponibles",
                    "message": "¿Qué horarios hay disponibles?",
                },
                {
                    "label": "Reservar",
                    "sub": "Agendar un turno",
                    "message": "Quiero reservar un turno",
                },
                {
                    "label": "Mis turnos",
                    "sub": "Ver mis reservas",
                    "message": "Quiero consultar mis turnos",
                },
            ],
        },
        "theme": palette,
    }
