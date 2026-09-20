"""Lógica y constantes de rate limiting (Bloque 5B, Paso 5).

Guards por ip/teléfono/usuario y ventana (estado) extraídos de app.py,
junto con la constante RATE_LIMIT_MAX_KEYS (re-exportada desde extensions).
"""

from application.requests import _rate_limit_key
from extensions import RATE_LIMIT_MAX_KEYS, _is_request_allowed
from services.appointments import normalize_phone

CHAT_REQUEST_LIMIT = 20
CHAT_PHONE_REQUEST_LIMIT = 20
API_REQUEST_LIMIT = 60
FORGOT_REQUEST_LIMIT = 5
REGISTRO_REQUEST_LIMIT = 3


def is_chat_request_allowed(client_ip, business_id=None):
    return _is_request_allowed(_rate_limit_key("chat", client_ip, business_id), CHAT_REQUEST_LIMIT)


def is_chat_phone_request_allowed(customer_phone, business_id=None):
    phone = normalize_phone(customer_phone) if customer_phone else ""
    if not phone:
        return True
    scope = f"phone:{phone}:business:{business_id}"
    key = f"chat:{scope}"
    return _is_request_allowed(key, CHAT_PHONE_REQUEST_LIMIT)


def is_api_request_allowed(client_ip, endpoint="api", business_id=None, user_id=None):
    return _is_request_allowed(
        _rate_limit_key(endpoint, client_ip, business_id, user_id), API_REQUEST_LIMIT
    )


def is_login_request_allowed(client_ip):
    return _is_request_allowed(_rate_limit_key("login", client_ip), 10)