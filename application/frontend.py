"""Contrato público del frontend y límites de chat (Bloque 5B, Paso 5).

build_public_frontend_config y las constantes de límites de mensajes/chat
extraídos de app.py, manteniendo lógica idéntica.
"""

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
            "logo_url": value("logo_url", ""),
            "primary_color": value("primary_color", "#1463FF"),
            "secondary_color": value("secondary_color", "#0B1B3A"),
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
        "theme": {
            "primary": value("primary_color", "#1463FF"),
            "secondary": value("secondary_color", "#0B1B3A"),
            "background": "#EAF4FF",
            "text": "#102A56",
            "font_family": "DM Sans",
        },
    }
