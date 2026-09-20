"""
app.py — FACHADA de la aplicación (Bloque 5B).

El core vive en el paquete `application` (application/__init__.py -> create_app)
y en los módulos por dominio (tenant, requests, rate_limit, notifications,
frontend, security, platform, config, logging_config, session_crypto).

Este módulo construye la instancia única `app` (via create_app) y re-exporta
los nombres que routes y tests consumen vía `from app import ...` /
`application.<name>`, preservando compatibilidad en comportamiento e identidad
de objetos (los imports se realizan ANTES de create_app para que
routes/auth_public.py, con 'from app import ...' en el top-level, encuentre los
nombres durante el registro de blueprints).
"""

from dotenv import load_dotenv
from flask import g, request

load_dotenv()

from application.logging_config import (
    USE_JSON_LOGS,
    RequestContextFilter,
    RequestIdFilter,
    StructuredFormatter,
    configure_logging,
    logger,
)

configure_logging()

# Infraestructura compartida (identidad con extensions)
from extensions import (
    RATE_LIMIT_MAX_KEYS,
    _is_request_allowed,
    _prune_rate_limit_state,
    csrf_token,
    rate_limit_state,
)

from services.ai import ask_ai
from services.notifications import send_confirmation_email

from database.database import (
    get_business_settings,
    get_business_settings_scoped,
    get_connection,
    init_database,
)

# Helpers de dominio (compat con tests y rutas)
from application.session_crypto import _hash_session_token, _now_iso

from application.tenant import (
    _settings_value,
    get_current_business_id,
    inject_admin_prefix,
    inject_business_settings,
    load_current_business,
    resolve_business,
)

from application.frontend import (
    MAX_HISTORY_CONTENT_LENGTH,
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_LENGTH,
    build_public_frontend_config,
)

from application.rate_limit import (
    API_REQUEST_LIMIT,
    CHAT_PHONE_REQUEST_LIMIT,
    CHAT_REQUEST_LIMIT,
    FORGOT_REQUEST_LIMIT,
    REGISTRO_REQUEST_LIMIT,
    is_api_request_allowed,
    is_chat_phone_request_allowed,
    is_chat_request_allowed,
    is_login_request_allowed,
)

from application.requests import (
    _human_reschedule_error,
    _rate_limit_key,
    get_client_ip,
    json_object,
)

from application.notifications import (
    send_approved_invitation_email,
    send_business_confirmation_email,
)

from application.platform import (
    _clear_platform_session,
    _is_platform_authenticated,
    _platform_actor_email,
    _platform_current_user,
    _platform_session_expires_at,
)

from application.security import (
    _ERROR_MESSAGES,
    _html_error,
    _is_json_request,
    _json_error,
    _reject_oversized_requests,
    add_security_headers,
    handle_400,
    handle_403,
    handle_404,
    handle_405,
    handle_413,
    handle_429,
    handle_500,
    handle_unhandled_exception,
)

# Auth helpers (BLOQUE 1): re-importados desde routes/auth.py
from routes.auth import (
    _establish_session,
    _is_authenticated,
    _login_url,
    _authenticate_login,
    login_required,
)

# Instancia de la aplicación (app factory)
from application import create_app

app = create_app()

# Valores de configuración re-exportados
ADMIN_PASSWORD_HASH = app.ADMIN_PASSWORD_HASH
ADMIN_PASSWORD = app.ADMIN_PASSWORD
TRUSTED_PROXY_COUNT = app.TRUSTED_PROXY_COUNT


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=5000)
