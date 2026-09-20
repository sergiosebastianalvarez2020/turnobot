import os
import re
import uuid
import logging
import json
from logging.handlers import RotatingFileHandler
import hashlib
import secrets
import math
import threading
from collections import defaultdict, deque
from functools import wraps
from time import monotonic
import datetime
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

from dotenv import load_dotenv
from flask import Flask, abort, g, render_template, request, jsonify, session, redirect, url_for, has_request_context

from extensions import _is_request_allowed, _prune_rate_limit_state, rate_limit_state, _RATE_LIMIT_LOCK, RATE_LIMIT_MAX_KEYS, RATE_LIMIT_WINDOW_SECONDS, csrf_token, valid_csrf_token

from services.ai import ask_ai
from services.notifications import (
    send_confirmation_email,
    notifications_enabled,
)
from services.conversations import (
    get_conversation_messages_by_public_token_scoped,
)
from database.database import (
    get_business_settings,
    get_business_settings_scoped,
    get_connection,
    init_database,
    update_appointment_status,
    update_appointment_status_scoped,
    update_business_settings,
    get_active_services_scoped,
    get_all_services_scoped,
    create_service_scoped,
    update_service_scoped,
    get_resources_scoped,
    get_resource_scoped,
    get_or_create_loyalty_account_scoped,
)

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
    validate_email,
    get_appointment_by_token,
    get_appointments,
    normalize_phone,
)
from database.database import (
    get_user_by_email_scoped,
    get_user_by_id_scoped,
    get_membership_scoped,
    is_session_valid_scoped,
)

from database.seed_auth import migrate_owner_from_module_hash
from services import memberships
from services import loyalty
from services import product
from database.database import (
    ensure_loyalty_settings_scoped,
    get_or_create_loyalty_account_scoped,
)
from services import notifications as notifications_service
from database.database import (
    get_platform_user_by_id,
    is_platform_session_valid,
)


load_dotenv()

USE_JSON_LOGS = os.getenv("LOG_FORMAT", "plain").lower() == "json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
)

log_dir = os.getenv("LOG_DIR", "logs")
os.makedirs(log_dir, exist_ok=True)
file_handler = RotatingFileHandler(
    os.path.join(log_dir, "app.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
)


class RequestContextFilter(logging.Filter):
    """Inyecta contexto HTTP en cada registro de log (request_id, business_id, endpoint, method, path)."""

    def filter(self, record):
        if has_request_context():
            try:
                existing_rid = getattr(record, "request_id", None)
                if existing_rid and existing_rid != "-":
                    record.request_id = existing_rid
                else:
                    record.request_id = getattr(g, "request_id", None) or request.headers.get("X-Request-ID", "").strip() or "-"
            except Exception:
                record.request_id = "-"

            try:
                existing_bid = getattr(record, "business_id", None)
                if existing_bid and existing_bid != "-":
                    record.business_id = str(existing_bid)
                else:
                    business_id = getattr(g, "business_id", None)
                    if business_id is None:
                        curr = getattr(g, "current_business", None)
                        if isinstance(curr, dict):
                            business_id = curr.get("id")
                        elif curr is not None:
                            try:
                                business_id = curr["id"]
                            except Exception:
                                business_id = None
                    record.business_id = str(business_id) if business_id is not None else "-"
            except Exception:
                record.business_id = "-"

            try:
                record.endpoint = getattr(g, "endpoint", None) or request.endpoint or "-"
            except Exception:
                record.endpoint = "-"

            try:
                record.method = getattr(g, "method", None) or request.method or "-"
            except Exception:
                record.method = "-"

            try:
                record.path = getattr(g, "path", None) or request.path or "-"
            except Exception:
                record.path = "-"
        else:
            if not hasattr(record, "request_id") or getattr(record, "request_id") is None:
                record.request_id = "-"
            if not hasattr(record, "business_id") or getattr(record, "business_id") is None:
                record.business_id = "-"
            if not hasattr(record, "endpoint") or getattr(record, "endpoint") is None:
                record.endpoint = "-"
            if not hasattr(record, "method") or getattr(record, "method") is None:
                record.method = "-"
            if not hasattr(record, "path") or getattr(record, "path") is None:
                record.path = "-"
        return True


RequestIdFilter = RequestContextFilter


class StructuredFormatter(logging.Formatter):
    """Formatter que emite logs en JSON o texto consistente con contexto enriquecido."""

    def format(self, record):
        try:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        req_id = getattr(record, "request_id", "-")
        biz_id = getattr(record, "business_id", "-")
        endpoint = getattr(record, "endpoint", "-")
        method = getattr(record, "method", "-")
        path = getattr(record, "path", "-")

        log_data = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": req_id,
            "business_id": biz_id,
            "endpoint": endpoint,
            "method": method,
            "path": path,
        }
        for attr in ("status_code", "latency_ms"):
            val = getattr(record, attr, None)
            if val is not None:
                log_data[attr] = val

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        if USE_JSON_LOGS:
            return json.dumps(log_data)

        context_part = f"[{req_id}] [b:{biz_id}] [{method} {endpoint}]"
        extra_info = ""
        if hasattr(record, "status_code") or hasattr(record, "latency_ms"):
            status_val = getattr(record, "status_code", "-")
            lat_val = getattr(record, "latency_ms", "-")
            extra_info = f" (status={status_val} lat={lat_val}ms)"
        exc_part = f"\n{self.formatException(record.exc_info)}" if record.exc_info else ""
        return f"{ts} {record.levelname} {record.name} {context_part} {record.getMessage()}{extra_info}{exc_part}"


file_handler.setFormatter(StructuredFormatter())

for handler in logging.getLogger().handlers:
    handler.addFilter(RequestContextFilter())
    if USE_JSON_LOGS:
        handler.setFormatter(StructuredFormatter())
file_handler.addFilter(RequestContextFilter())
logging.getLogger().addHandler(file_handler)

logger = logging.getLogger("el_corte.web")

if os.getenv("FLASK_ENV") == "production" and not os.getenv("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY es obligatoria en producción")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if os.getenv("FLASK_ENV") == "production" and ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD fue eliminado; use ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH:
    logger.warning("ADMIN_PASSWORD_HASH no está configurada: acceso administrativo deshabilitado")
if os.getenv("FLASK_ENV") == "production" and (not ADMIN_PASSWORD_HASH or os.getenv("COOKIE_SECURE") != "1"):
    raise RuntimeError("ADMIN_PASSWORD_HASH es obligatoria en producción")
if os.getenv("FLASK_ENV") == "production" and not os.getenv("SMTP_HOST"):
    logger.warning(
        "SMTP_HOST no está configurada: las notificaciones de turnos no se enviarán por email. "
        "Configure SMTP_HOST/SMTP_USER/SMTP_PASSWORD e incree el servidor."
    )
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024

# X-Forwarded-* is trusted only when an explicitly configured reverse proxy is
# in front of the application.  With the default of zero, request.remote_addr
# remains the peer address and client-supplied forwarding headers are ignored.
try:
    TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
except ValueError as error:
    raise RuntimeError("TRUSTED_PROXY_COUNT debe ser un entero >= 0") from error
if TRUSTED_PROXY_COUNT < 0:
    raise RuntimeError("TRUSTED_PROXY_COUNT debe ser un entero >= 0")
if TRUSTED_PROXY_COUNT:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=TRUSTED_PROXY_COUNT,
        x_proto=TRUSTED_PROXY_COUNT,
        x_host=TRUSTED_PROXY_COUNT,
        x_port=TRUSTED_PROXY_COUNT,
        x_prefix=TRUSTED_PROXY_COUNT,
    )

default_lifetime = int(os.getenv("SESSION_LIFETIME_SECONDS", "86400"))
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(seconds=default_lifetime)

init_database()


def resolve_business(slug=None):
    """Resuelve un negocio existente sin aceptar un identificador del cliente."""
    connection = get_connection()
    try:
        if slug is None:
            row = connection.execute(
                "SELECT id, name, slug FROM businesses WHERE id = 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id, name, slug FROM businesses WHERE slug = ? AND active = 1",
                (slug,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_current_business_id():
    """Devuelve el negocio asociado al request actual, si existe."""
    business = getattr(g, "current_business", None)
    return business["id"] if business else None


@app.before_request
def load_current_business():
    """Carga el contexto request-scoped en función del slug de la URL o del fallback por defecto."""
    view_args = request.view_args or {}
    slug = view_args.get("slug")

    # request_id
    request_id = request.headers.get("X-Request-ID", "").strip()
    if request_id:
        request_id = request_id[:64]
    else:
        request_id = uuid.uuid4().hex
    g.request_id = request_id
    g.endpoint = request.endpoint or "-"
    g.method = request.method
    g.path = request.path
    g.start_time = monotonic()

    if request.path.startswith("/b/"):
        if slug is None:
            g.current_business = None
            g.business_id = None
            return abort(404)
        g.current_business = resolve_business(slug)
        if g.current_business is None:
            if hasattr(g, "current_business"):
                delattr(g, "current_business")
            g.business_id = None
            return abort(404)
        g.business_id = g.current_business.get("id") if isinstance(g.current_business, dict) else (g.current_business["id"] if g.current_business else None)
        return None

    g.current_business = resolve_business()
    if g.current_business:
        g.business_id = g.current_business.get("id") if isinstance(g.current_business, dict) else (g.current_business["id"] if g.current_business else None)
    else:
        g.business_id = None
    return None


@app.context_processor
def inject_admin_prefix():
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1 and business.get("slug"):
        admin_prefix = f"/b/{business['slug']}"
    else:
        admin_prefix = ""
    return {"admin_prefix": admin_prefix}


def _settings_value(settings, key, default=""):
    """Acceso seguro a una clave de settings (dict o sqlite3.Row).

    sqlite3.Row no soporta .get(); usa [] con try/except KeyError.
    """
    if not settings:
        return default
    try:
        return settings[key] or default
    except (KeyError, IndexError, TypeError):
        return default


@app.context_processor
def inject_business_settings():
    business_id = get_current_business_id()
    settings = (
        get_business_settings_scoped(business_id)
        if business_id is not None
        else get_business_settings()
    )
    return {
        "business_settings": settings,
        "business_name": _settings_value(settings, "business_name", "Mi negocio"),
        "business_type": _settings_value(settings, "business_type", "Negocio"),
        "business_initials": _settings_value(settings, "business_initials", ""),
        "business_description": _settings_value(settings, "business_description", ""),
        "timezone": _settings_value(settings, "timezone", "UTC"),
        "logo_url": _settings_value(settings, "logo_url", ""),
        "primary_color": _settings_value(settings, "primary_color", ""),
        "secondary_color": _settings_value(settings, "secondary_color", ""),
    }

MAX_MESSAGE_LENGTH = 1_000


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
                {"label": "Servicios", "sub": "Ver todos los servicios", "message": "¿Qué servicios tienen y cuánto cuestan?"},
                {"label": "Disponibilidad", "sub": "Ver horarios disponibles", "message": "¿Qué horarios hay disponibles?"},
                {"label": "Reservar", "sub": "Agendar un turno", "message": "Quiero reservar un turno"},
                {"label": "Mis turnos", "sub": "Ver mis reservas", "message": "Quiero consultar mis turnos"},
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


MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_LENGTH = 2_000
CHAT_REQUEST_LIMIT = 20
CHAT_PHONE_REQUEST_LIMIT = 20
API_REQUEST_LIMIT = 60


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers.setdefault("X-Request-ID", request_id)
    if os.getenv("FLASK_ENV") == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    # Correlación de petición HTTP (omitir /static/ para evitar ruido)
    if not request.path.startswith("/static/"):
        start_time = getattr(g, "start_time", None)
        latency_ms = round((monotonic() - start_time) * 1000, 2) if start_time else 0.0
        logger.info(
            "HTTP %s %s -> %s (%sms)",
            request.method,
            request.path,
            response.status_code,
            latency_ms,
            extra={
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )
    return response


# ============================================================
# HANDLERS GLOBALES DE ERRORES
# ============================================================

_ERROR_MESSAGES = {
    400: "Solicitud inválida.",
    403: "Acceso prohibido.",
    404: "Recurso no encontrado.",
    405: "Método no permitido.",
    429: "Demasiadas solicitudes. Intentá nuevamente en unos momentos.",
    413: "Solicitud demasiado grande.",
    500: "Error interno del servidor.",
}


def _is_json_request():
    return (
        request.is_json
        or request.path.startswith("/api/")
        or "/api/" in request.path
        or (request.headers.get("Accept", "") and "json" in request.headers.get("Accept", ""))
    )


def _json_error(code, message):
    return jsonify({
        "success": False,
        "error": message,
        "code": code,
        "request_id": getattr(g, "request_id", None) or "-",
    })


def _html_error(code, message):
    return render_template(
        "error.html",
        code=code,
        message=message,
        request_id=getattr(g, "request_id", None) or "-",
    ), code


@app.errorhandler(400)
def handle_400(error):
    message = getattr(error, "description", None) or _ERROR_MESSAGES[400]
    if _is_json_request():
        return _json_error("BAD_REQUEST", message), 400
    return _html_error(400, message)


@app.errorhandler(403)
def handle_403(error):
    message = getattr(error, "description", None) or _ERROR_MESSAGES[403]
    if _is_json_request():
        return _json_error("FORBIDDEN", message), 403
    return _html_error(403, message)


@app.errorhandler(404)
def handle_404(error):
    message = getattr(error, "description", None) or _ERROR_MESSAGES[404]
    if not message or "The requested URL was not found" in message:
        message = _ERROR_MESSAGES[404]
    if _is_json_request():
        return _json_error("NOT_FOUND", message), 404
    return _html_error(404, message)


@app.errorhandler(405)
def handle_405(error):
    message = "Método no permitido."
    if _is_json_request():
        return _json_error("METHOD_NOT_ALLOWED", message), 405
    return _html_error(405, message)


@app.errorhandler(429)
def handle_429(error):
    message = _ERROR_MESSAGES[429]
    if _is_json_request():
        return _json_error("RATE_LIMITED", message), 429
    return _html_error(429, message)


@app.errorhandler(413)
def handle_413(error):
    message = _ERROR_MESSAGES[413]
    if _is_json_request():
        return _json_error("PAYLOAD_TOO_LARGE", message), 413
    return _html_error(413, message)


@app.before_request
def _reject_oversized_requests():
    max_length = app.config.get("MAX_CONTENT_LENGTH")
    if max_length and (request.content_length or 0) > max_length:
        abort(413)


@app.errorhandler(500)
def handle_500(error):
    logger.exception("Error 500: %s", getattr(error, "description", str(error)))
    message = _ERROR_MESSAGES[500]
    if _is_json_request():
        return _json_error("INTERNAL_ERROR", message), 500
    return _html_error(500, message)


@app.errorhandler(Exception)
def handle_unhandled_exception(error):
    if isinstance(error, HTTPException):
        return error
    logger.exception("Excepción no controlada: %s", error)
    message = _ERROR_MESSAGES[500]
    if _is_json_request():
        return _json_error("INTERNAL_ERROR", message), 500
    return _html_error(500, message)


app.jinja_env.globals["csrf_token"] = csrf_token


def get_client_ip():
    return request.remote_addr or "unknown"


def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _rate_limit_key(endpoint, client_ip, business_id=None, user_id=None):
    # Values come only from Flask's resolved request/session context, never
    # from request data supplied by the caller.
    scope = f"user:{user_id}:business:{business_id}" if user_id else f"ip:{client_ip}:business:{business_id}"
    return f"{endpoint}:{scope}"


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


# ============================================================
# RATE LIMIT CONSTANTS — referenced by routes/auth_public.py
# ============================================================

FORGOT_REQUEST_LIMIT = 5
REGISTRO_REQUEST_LIMIT = 3


# ============================================================
# AUTH HELPERS — migrados a routes/auth.py (BLOQUE 1)
# ============================================================
# login_required, membership_required, _is_authenticated, _login_url,
# _hash_session_token, _now_iso, _establish_session, _authenticate_login,
# login, login_slug, logout, logout_slug
#
# Se re-importan desde routes/auth.py para preservar compatibilidad con
# tests y código existente que accede vía app.login_required, etc.
from routes.auth import (
    login_required,
    _is_authenticated,
    _login_url,
    _hash_session_token,
    _now_iso,
    _establish_session,
    _authenticate_login,
)


# ============================================================
# SESIÓN DE PLATAFORMA (SUPERADMIN)
# ============================================================
#
# La identidad de plataforma (`platform_users`/`platform_sessions`) es
# COMPLETAMENTE independiente de la identidad de negocio. Se persisten en
# claves de sesión distintas (platform_user_id / platform_session_token) para
# que ambas identidades coexistan dentro de la misma cookie sin interferir.

_PLATFORM_SESSION_LIFETIME_SECONDS = int(
    os.getenv("PLATFORM_SESSION_LIFETIME_SECONDS", str(8 * 3600))
)


def _platform_session_expires_at():
    delta = datetime.timedelta(seconds=_PLATFORM_SESSION_LIFETIME_SECONDS)
    return (
        datetime.datetime.now(datetime.timezone.utc) + delta
    ).strftime("%Y-%m-%d %H:%M:%S")


def _clear_business_session():
    """Invalidate ONLY the business (tenant) session, preserving the platform one."""
    session.pop("user_id", None)
    session.pop("session_token", None)


def _clear_platform_session():
    """Invalidate ONLY the platform session, preserving the business one."""
    session.pop("platform_user_id", None)
    session.pop("platform_session_token", None)


def _platform_current_user():
    """Devuelve el SUPERADMIN autenticado (sin password_hash) o (None, None)."""
    platform_user_id = session.get("platform_user_id")
    token = session.get("platform_session_token")
    if not platform_user_id or not token:
        return None, None
    user = get_platform_user_by_id(platform_user_id)
    if user is None or not user["active"]:
        _clear_platform_session()
        return None, None
    if not is_platform_session_valid(
        platform_user_id, _hash_session_token(token), _now_iso()
    ):
        _clear_platform_session()
        return None, None
    safe_user = {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "active": bool(user["active"]),
    }
    return safe_user, token


def _is_platform_authenticated():
    user, _ = _platform_current_user()
    return user is not None


def _platform_actor_email():
    user, _ = _platform_current_user()
    return user["email"] if user else ""


# ============================================================
# NOTIFICACIONES — Wrappers para tests (se pueden mockear)
# ============================================================

def send_approved_invitation_email(owner_email, business_name, invitation_link, expires_at="", lifetime_hours=None):
    """Wrapper para EMAIL 2: invitación aprobada al owner."""
    return notifications_service.send_approved_invitation_email(
        owner_email, business_name, invitation_link, expires_at, lifetime_hours
    )


def send_business_confirmation_email(business_id, appointment, force=False):
    """Wrapper para EMAIL 5: aviso de reserva al negocio."""
    return notifications_service.send_business_confirmation_email(business_id, appointment, force)


# ============================================================
# CONFIGURACIÓN DEL NEGOCIO
# ============================================================

def get_active_services():
    """
    Obtiene los servicios activos del negocio actual.
    Si business_id no está disponible, retorna vacío (fallback seguro).
    """
    from database.database import get_active_services_scoped

    business_id = get_current_business_id()
    if not business_id:
        return {}

    try:
        rows = get_active_services_scoped(business_id)
        if not rows:
            return {}
        
        services = {}
        for row in rows:
            services[row["name"]] = {
                "price": row["price"],
                "duration": row["duration"],
            }
        return services
    except Exception:
        logger.exception("Error obteniendo servicios activos")
        return {}


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================
# Las rutas / y /b/<slug> están definidas en routes/public.py
# y registradas vía register_blueprints() al final de este archivo.

# ============================================================
# LOGIN / LOGOUT / SESSION — migrados a routes/auth.py (BLOQUE 1)
# ============================================================
# Las rutas login, login_slug, logout, logout_slug están definidas en
# routes/auth.py y registradas vía register_blueprints() al final de este
# archivo. Los helpers de sesión están importados desde routes.auth arriba.


# ============================================================
def _human_reschedule_error(reason):
    reasons = {
        "occupied": "ese horario ya está ocupado.",
        "past_date": "no podés reprogramar a una fecha que ya pasó.",
        "closed_day": "ese día estamos cerrados.",
        "invalid_date": "la fecha no es válida.",
        "invalid_time": "el horario no es válido.",
        "not_found": "no se encontró el turno.",
        "invalid_phone": "el teléfono no es válido.",
    }
    return reasons.get(reason, "intentá nuevamente.")


# ============================================================


# REGISTRO DE BLUEPRINTS
# ============================================================

from routes import register_blueprints

register_blueprints(app)


# ============================================================
# INICIAR SERVIDOR
# ============================================================

if __name__ == "__main__":

    app.run(

        debug=False,

        host="127.0.0.1",

        port=5000
    )
