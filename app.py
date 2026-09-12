import os
import logging
from logging.handlers import RotatingFileHandler
import hashlib
import secrets
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import defaultdict, deque
from functools import wraps
from time import monotonic
import datetime
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from dotenv import load_dotenv
from flask import Flask, abort, g, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from services.ai import ask_ai
from services.notifications import send_confirmation_email, smtp_configured, notifications_enabled
from services.knowledge import (
    get_knowledge_scoped,
    search_knowledge_scoped,
    create_knowledge_scoped,
    update_knowledge_scoped,
    delete_knowledge_scoped,
    get_knowledge_by_id_scoped,
)
from services.conversations import (
    get_or_create_conversation_session_scoped,
    add_conversation_message_scoped,
    get_conversation_session_by_id_scoped,
    list_conversation_sessions_scoped,
    count_conversation_sessions_scoped,
    get_conversation_messages_scoped,
    request_human_handoff_scoped,
    resolve_human_handoff_scoped,
    update_session_status_scoped,
    get_needs_human_sessions_scoped,
    get_frequent_questions_scoped,
    get_unanswered_questions_scoped,
    get_conversation_stats_scoped,
    track_question_scoped,
    get_opportunities_scoped,
)
from database.database import (
    get_business_settings,
    get_business_settings_scoped,
    get_connection,
    init_database,
    update_appointment_status,
    update_appointment_status_scoped,
    update_business_settings,
    update_business_settings_scoped,
    get_active_services_scoped,
    get_all_services_scoped,
    create_service_scoped,
    update_service_scoped,
    get_resources_scoped,
    get_resource_scoped,
    create_resource_scoped,
    set_resource_active_scoped,
    get_all_weekly_schedules_scoped,
    update_weekly_schedule_scoped,
    get_weekly_schedule_scoped,
)

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
    reschedule_appointment_admin,
    validate_email,
    get_appointment_by_token,
    get_appointments,
    get_appointment_counts,
)
from database.database import (
    get_user_by_email_scoped,
    get_user_by_id_scoped,
    get_membership_scoped,
    create_session_scoped,
    revoke_all_sessions_scoped,
    is_session_valid_scoped,
)
from database.database import (
    create_user_scoped,
)
from database.seed_auth import migrate_owner_from_module_hash
from services import memberships
from services import loyalty
from services import product
from database.database import (
    get_loyalty_settings_scoped,
    ensure_loyalty_settings_scoped,
    update_loyalty_settings_scoped,
    get_or_create_loyalty_account_scoped,
    get_loyalty_account_by_id_scoped,
    recalculate_all_balances_scoped,
    list_points_ledger_scoped,
    list_loyalty_accounts_scoped,
)
from services import platform as platform_service
from services import notifications as notifications_service
from database.database import (
    get_platform_user_by_id,
    is_platform_session_valid,
    revoke_all_platform_sessions,
    list_members_scoped,
)


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log_dir = os.getenv("LOG_DIR", "logs")
os.makedirs(log_dir, exist_ok=True)
file_handler = RotatingFileHandler(
    os.path.join(log_dir, "app.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
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
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)

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
    if request.path.startswith("/b/"):
        # Proteger el caso en que Flask no hizo match de ruta (view_args=None).
        # Ej: /b/<slug>/admin/login no existe como ruta registrada → 404 seguro, nunca 500.
        view_args = request.view_args or {}
        slug = view_args.get("slug")
        if not slug:
            return abort(404)
        g.current_business = resolve_business(slug)
        if g.current_business is None:
            if hasattr(g, "current_business"):
                delattr(g, "current_business")
            return abort(404)
        return None

    g.current_business = resolve_business()
    return None


@app.context_processor
def inject_admin_prefix():
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1 and business.get("slug"):
        admin_prefix = f"/b/{business['slug']}"
    else:
        admin_prefix = ""
    return {"admin_prefix": admin_prefix}


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
        "business_name": settings["business_name"] if settings else "Mi negocio",
        "business_type": settings["business_type"] if settings else "Negocio",
        "business_initials": settings["business_initials"] if settings else "",
        "business_description": settings["business_description"] if settings else "",
        "timezone": settings["timezone"] if settings else "UTC",
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
            "primary": "#1463FF",
            "secondary": "#0B1B3A",
            "background": "#EAF4FF",
            "text": "#102A56",
            "font_family": "DM Sans",
        },
    }


MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_LENGTH = 2_000
CHAT_REQUEST_LIMIT = 20
API_REQUEST_LIMIT = 60
RATE_LIMIT_WINDOW_SECONDS = 60

rate_limit_state = defaultdict(deque)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if os.getenv("FLASK_ENV") == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf_token(value):
    return bool(value) and secrets.compare_digest(value, session.get("csrf_token", ""))


app.jinja_env.globals["csrf_token"] = csrf_token


def _is_request_allowed(key, limit):
    now = monotonic()
    requests = rate_limit_state[key]
    while requests and now - requests[0] > RATE_LIMIT_WINDOW_SECONDS:
        requests.popleft()
    if len(requests) >= limit:
        return False
    requests.append(now)
    return True


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


def is_api_request_allowed(client_ip, endpoint="api", business_id=None, user_id=None):
    return _is_request_allowed(
        _rate_limit_key(endpoint, client_ip, business_id, user_id), API_REQUEST_LIMIT
    )


def is_login_request_allowed(client_ip):
    return _is_request_allowed(_rate_limit_key("login", client_ip), 10)


def _hash_session_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _login_url():
    business_id = get_current_business_id()
    if business_id == 1:
        return url_for("login")
    return url_for("login_slug", slug=g.current_business["slug"])


def _is_authenticated():
    """Valida una sesión activa (usuario y sesión persistente válida para el negocio actual)."""
    user_id = session.get("user_id")
    token = session.get("session_token")
    if not user_id or not token:
        return False

    user = get_user_by_id_scoped(user_id)
    if not user or not user["active"]:
        return False

    # La sesión persistente también está restringida al negocio actual:
    # se crea tras autenticar contra business_users, y se revoca en logout.
    if not is_session_valid_scoped(user_id, _hash_session_token(token), _now_iso()):
        return False

    return True


def login_required(f):
    """Requiere una sesión activa (sin restringir el negocio objetivo)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authenticated():
            session.clear()
            return redirect(_login_url())
        return f(*args, **kwargs)
    return decorated


def membership_required(*role_names):
    """Requiere que el usuario autenticado tenga una membresía con rol(es)
    permitido(s) en el negocio resuelto por el slug de la URL (nunca del cliente)."""
    allowed = set(role_names)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not _is_authenticated():
                session.clear()
                return redirect(_login_url())

            user_id = session.get("user_id")
            business_id = get_current_business_id()
            membership = get_membership_scoped(user_id, business_id) if business_id else None
            if not membership or membership["role_name"] not in allowed:
                session.clear()
                return redirect(_login_url())

            return f(*args, **kwargs)
        return decorated
    return decorator


def _require_admin_membership():
    """
    Comprueba autenticación + membresía administrativa (owner/admin) para el
    negocio resuelto por el slug. Retorna None si es válido, o una respuesta
    redirigida de login si no.
    """
    if not _is_authenticated():
        session.clear()
        return redirect(_login_url())
    user_id = session.get("user_id")
    business_id = get_current_business_id()
    membership = get_membership_scoped(user_id, business_id) if business_id else None
    if not membership or membership["role_name"] not in {"owner", "admin"}:
        session.clear()
        return redirect(_login_url())
    return None


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

def send_apply_confirmation_email(owner_email, business_name):
    """Wrapper para EMAIL 1: confirmación de recepción de alta."""
    return notifications_service.send_apply_confirmation_email(owner_email, business_name)


def send_apply_notice_to_superadmin(business_name, business_id=None):
    """Wrapper para EMAIL 1: aviso a superadmins de alta pendiente."""
    return notifications_service.send_apply_notice_to_superadmin(business_name, business_id)


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

@app.route("/")
def index():
    business_id = get_current_business_id()
    settings = (
        get_business_settings_scoped(business_id)
        if business_id is not None
        else get_business_settings()
    )
    return render_template(
        "index.html",
        public_frontend_config=build_public_frontend_config(settings),
    )


@app.route("/b/<slug>")
def business_index(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)

    business_id = get_current_business_id()
    settings = (
        get_business_settings_scoped(business_id)
        if business_id is not None
        else get_business_settings()
    )
    return render_template(
        "index.html",
        public_frontend_config=build_public_frontend_config(settings),
    )


@app.route("/health", methods=["GET"])
def health():
    try:
        connection = get_connection()
        connection.execute("SELECT 1").fetchone()
        connection.close()
        return jsonify({"status": "ok", "database": "ok"})
    except Exception:
        logger.exception("Health check failed")
        return jsonify({"status": "error", "database": "error"}), 503


# ============================================================
# LOGIN / LOGOUT ADMIN
# ============================================================

def _establish_session(user_id):
    """Crea una sesión persistente tras un login exitoso.

    Limpia selectivamente la sesión de NEGOCIO (user_id/session_token) pero
    PRESERVA la sesión de PLATAFORMA (superadmin) y el token CSRF. Esto permite
    que la sesión de negocio y la de superadmin coexistan dentro de la misma
    cookie cuando un mismo agente las utiliza de forma alternada.
    """
    old_csrf = session.get("csrf_token")
    platform_user_id = session.get("platform_user_id")
    platform_token = session.get("platform_session_token")
    session.pop("user_id", None)
    session.pop("session_token", None)
    session["user_id"] = user_id
    token = secrets.token_urlsafe(48)
    session["session_token"] = token
    session.permanent = True
    lifetime = app.config["PERMANENT_SESSION_LIFETIME"]
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc) + lifetime
    ).strftime("%Y-%m-%d %H:%M:%S")
    create_session_scoped(user_id, _hash_session_token(token), expires_at)
    # mantenemos el mismo token CSRF para no invalidar formularios ya abiertos
    session["csrf_token"] = old_csrf or csrf_token()
    # restauramos la sesión de plataforma si existía (coexistencia de contextos)
    if platform_user_id is not None:
        session["platform_user_id"] = platform_user_id
    if platform_token is not None:
        session["platform_session_token"] = platform_token


def _authenticate_login(business, email, password):
    """
    Autentica email+password contra users/business_users para el negocio dado.
    Retorna (user_id, None) o (None, mensaje_error).
    """
    if not is_login_request_allowed(get_client_ip()):
        return None, "Demasiados intentos. Esperá unos minutos."

    if business is None:
        return None, "Negocio no encontrado."

    email = (email or "").strip().lower()
    user = get_user_by_email_scoped(email) if email else None

    if user is not None:
        if not user["active"]:
            return None, "Credenciales inválidas."
        membership = get_membership_scoped(user["id"], business["id"])
        if not membership or membership["role_name"] == "customer":
            return None, "No tenés acceso administrativo a este negocio."
        if not password or not check_password_hash(user["password_hash"], password):
            return None, "Credenciales inválidas."
        return user["id"], None

    # --- Bootstrap de migración (negocio 1 solamente) -------------------------
    # Convierte la credencial administrativa histórica (module/env
    # ADMIN_PASSWORD_HASH) en el owner del negocio 1 dentro del modelo
    # users/business_users. MECANISMO DE MIGRACIÓN: una vez migrado, la
    # autenticación normal usa users/business_users/sessions.
    if business["id"] == 1 and ADMIN_PASSWORD_HASH:
        if password and check_password_hash(ADMIN_PASSWORD_HASH, password):
            user_id = migrate_owner_from_module_hash(
                1, email or "admin@turnobot.local", ADMIN_PASSWORD_HASH
            )
            if user_id is not None:
                return user_id, None
    return None, "Credenciales inválidas."


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template("login.html", error=True, error_message="La sesión expiró. Intentá nuevamente."), 400
        user_id, error = _authenticate_login(
            g.current_business,
            request.form.get("email", ""),
            request.form.get("password", ""),
        )
        if user_id is None:
            return render_template("login.html", error=True, error_message=error), (
                429 if error == "Demasiados intentos. Esperá unos minutos." else 200
            )
        _establish_session(user_id)
        return redirect(url_for("admin"))

    if session.get("user_id"):
        return redirect(url_for("admin"))
    return render_template("login.html", error=False)


@app.route("/b/<slug>/login", methods=["GET", "POST"])
def login_slug(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)

    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template("login.html", error=True, error_message="La sesión expiró. Intentá nuevamente."), 400
        user_id, error = _authenticate_login(
            business,
            request.form.get("email", ""),
            request.form.get("password", ""),
        )
        if user_id is None:
            return render_template("login.html", error=True, error_message=error), (
                429 if error == "Demasiados intentos. Esperá unos minutos." else 200
            )
        _establish_session(user_id)
        return redirect(url_for("admin_slug", slug=business["slug"]))

    if session.get("user_id"):
        return redirect(url_for("admin_slug", slug=business["slug"]))
    return render_template("login.html", error=False)


@app.route("/logout", methods=["POST"])
def logout():
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user_id = session.get("user_id")
    if user_id:
        revoke_all_sessions_scoped(user_id)
    _clear_business_session()
    return redirect(url_for("login"))


@app.route("/b/<slug>/logout", methods=["POST"])
def logout_slug(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user_id = session.get("user_id")
    if user_id:
        revoke_all_sessions_scoped(user_id)
    _clear_business_session()
    return redirect(url_for("login_slug", slug=business["slug"]))


# ============================================================
# SUPERADMIN — Capa HTTP sobre services.platform
# ============================================================

@app.route("/superadmin/login", methods=["GET", "POST"])
def superadmin_login():
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template("superadmin_login.html", error=True, error_message="Solicitud no válida"), 400
        email = (request.form.get("email", "") or "").strip().lower()
        password = request.form.get("password", "") or ""
        platform_user, error = platform_service.authenticate_superadmin(email, password)
        if platform_user is None:
            return render_template("superadmin_login.html", error=True, error_message=error or "Credenciales inválidas."), 200
        token = secrets.token_urlsafe(48)
        expires_at = _platform_session_expires_at()
        platform_service.establish_platform_session(platform_user["id"], token, expires_at)
        session["platform_user_id"] = platform_user["id"]
        session["platform_session_token"] = token
        platform_service.log_platform_action(platform_user["id"], platform_user["email"], None, "superadmin_login", "", get_client_ip())
        return redirect("/superadmin")

    user, _ = _platform_current_user()
    if user:
        return redirect("/superadmin")
    return render_template("superadmin_login.html", error=False)


@app.route("/superadmin/logout", methods=["POST"])
def superadmin_logout():
    user, token = _platform_current_user()
    if user is None:
        return redirect("/superadmin/login")
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    revoke_all_platform_sessions(user["id"])
    _clear_platform_session()
    platform_service.log_platform_action(user["id"], user["email"], None, "superadmin_logout", "", get_client_ip())
    return redirect("/superadmin/login")


def _superadmin_gate():
    """Gate de autenticación SUPERADMIN: redirige a login si no hay sesión de plataforma."""
    if not _is_platform_authenticated():
        return redirect("/superadmin/login")
    return None


@app.route("/superadmin")
def superadmin_panel():
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    message = request.args.get("message", "")
    error = request.args.get("error", "")
    businesses = platform_service.list_businesses_for_platform()
    return render_template(
        "superadmin.html",
        superadmin=user,
        businesses=businesses,
        section="businesses",
        message=message,
        error=error,
        platform_lifetime=platform_service.invitation_lifetime_hours(),
    )


@app.route("/superadmin/auditoria")
def superadmin_audit():
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    audit_rows = platform_service.list_audit_log()
    return render_template(
        "superadmin.html",
        superadmin=user,
        section="audit",
        audit_rows=audit_rows,
    )


@app.route("/superadmin/negocios/crear", methods=["POST"])
def superadmin_negocios_crear():
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    name = (request.form.get("nombre", "") or "").strip()
    slug = (request.form.get("slug", "") or "").strip().lower()
    owner_email = (request.form.get("owner_email", "") or "").strip().lower()
    user, _ = _platform_current_user()
    result = platform_service.provision_business(name, slug, owner_email)
    if not result["success"]:
        return redirect(f"/superadmin?error={result['reason']}")
    platform_service.log_platform_action(
        user["id"], user["email"], result["business_id"],
        "business_created", f"Negocio creado: {name} (slug: {result['slug']})",
        get_client_ip()
    )
    # EMAIL 1: confirmación al solicitante + aviso al superadmin
    send_apply_confirmation_email(owner_email, name)
    send_apply_notice_to_superadmin(name, result["business_id"])
    return redirect("/superadmin?message=Solicitud de alta registrada correctamente.")


@app.route("/superadmin/negocios/<int:business_id>")
def superadmin_negocios_detalle(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    business = platform_service.get_business_by_id_platform(business_id)
    if business is None:
        abort(404)
    business_settings = get_business_settings_scoped(business_id)
    if business_settings:
        business_settings = dict(business_settings)
    members = [dict(m) for m in list_members_scoped(business_id)]
    invitations = [dict(i) for i in platform_service.list_invitations(business_id)]
    message = request.args.get("message", "")
    error = request.args.get("error", "")
    return render_template(
        "superadmin.html",
        superadmin=user,
        section="business_detail",
        business=business,
        business_settings=business_settings,
        members=members,
        invitations=invitations,
        message=message,
        error=error,
    )


@app.route("/superadmin/negocios/<int:business_id>/aprobar", methods=["POST"])
def superadmin_negocios_aprobar(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    result = platform_service.approve_business(business_id)
    if not result["success"]:
        return redirect(f"/superadmin/negocios/{business_id}?error={result['reason']}")
    platform_service.log_platform_action(
        user["id"], user["email"], business_id,
        "business_approved", f"Negocio aprobado: {result['slug']} (slug: {result['slug']})",
        get_client_ip()
    )
    # EMAIL 2: invitación al owner para definir contraseña
    send_approved_invitation_email(
        result["owner_email"],
        result["slug"],
        request.url_root.rstrip("/") + result["invitation_url"],
        result.get("expires_at", ""),
        platform_service.invitation_lifetime_hours(),
    )
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio aprobado e invitación generada.")


@app.route("/superadmin/negocios/<int:business_id>/desactivar", methods=["POST"])
def superadmin_negocios_desactivar(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ok = platform_service.set_business_active(business_id, False)
    if not ok:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se pudo suspender el negocio.")
    platform_service.log_platform_action(user["id"], user["email"], business_id, "business_disabled", "Negocio suspendido", get_client_ip())
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio suspendido correctamente.")


@app.route("/superadmin/negocios/<int:business_id>/activar", methods=["POST"])
def superadmin_negocios_activar(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ok = platform_service.set_business_active(business_id, True)
    if not ok:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se pudo activar el negocio.")
    platform_service.log_platform_action(user["id"], user["email"], business_id, "business_enabled", "Negocio reactivado", get_client_ip())
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio activado correctamente.")


@app.route("/superadmin/negocios/<int:business_id>/reinviar", methods=["POST"])
def superadmin_negocios_reinviar(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    # buscar owner del negocio
    owner = None
    for m in [dict(m) for m in list_members_scoped(business_id)]:
        if m.get("role_name") == "owner":
            owner = m
            break
    if owner is None:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se encontró el owner del negocio.")
    token, expires_at = platform_service.resend_invitation(business_id, owner["user_id"], owner["email"])
    platform_service.log_platform_action(user["id"], user["email"], business_id, "invitation_resent", f"Nueva invitación enviada a {owner['email']}", get_client_ip())
    # EMAIL 2: nueva invitación al owner
    business = platform_service.get_business_by_id_platform(business_id)
    if business:
        send_approved_invitation_email(
            owner["email"],
            business["name"],
            request.url_root.rstrip("/") + f"/b/{business['slug']}/invitacion/{token}",
            expires_at,
            platform_service.invitation_lifetime_hours(),
        )
    return redirect(f"/superadmin/negocios/{business_id}?message=Nueva invitación generada.")


# ============================================================
# INVITACIÓN PÚBLICA (owner establece contraseña)
# ============================================================

@app.route("/b/<slug>/invitacion/<token>", methods=["GET", "POST"])
def public_invitation(slug, token):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)
    business_id = business["id"]
    invitation = platform_service.get_invitation_for_business(business_id, token)
    if invitation is None:
        return render_template("invitacion.html", business=business, invalid=True), 404

    if request.method == "GET":
        return render_template("invitacion.html", business=business, invitation=invitation, error=None, invalid=False)

    # POST: validar token antes que CSRF para que token vencido/usado devuelva 404
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")

    if password != password2:
        return render_template("invitacion.html", business=business, invitation=invitation, error="Las contraseñas no coinciden."), 200

    result = platform_service.accept_invitation(business_id, token, password)
    if not result["success"]:
        if result["reason"] == "weak_password":
            error = "La contraseña debe tener al menos 12 caracteres."
        elif result["reason"] == "invalid_token":
            error = "Enlace no válido o vencido."
        else:
            error = "No se pudo aceptar la invitación."
        return render_template("invitacion.html", business=business, invitation=invitation, error=error), 200

    return redirect(f"/b/{business['slug']}/login")


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
def admin():
    denied = _require_admin_membership()
    if denied:
        return denied
    return _render_admin()


@app.route("/b/<slug>/admin")
def admin_slug(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)
    denied = _require_admin_membership()
    if denied:
        return denied
    return _render_admin()


def _render_admin():
    business_id = get_current_business_id()
    settings = get_business_settings_scoped(business_id)
    if business_id is None:
        abort(404)
    if settings is None:
        settings = {
            "business_name": "Mi negocio",
            "business_initials": "",
            "business_type": "Negocio",
            "business_description": "",
            "timezone": "UTC",
            "slot_duration": 60,
            "break_between_slots": 0,
            "notifications_enabled": 0,
            "notification_email": "",
        }
    services = get_all_services_scoped(business_id)
    onboarding = product.get_onboarding_state(business_id, settings, services)
    status = request.args.get("status") or "confirmed"
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        status = "confirmed"
    appointment_date = request.args.get("fecha") or None
    actor_user_id = session.get("user_id")
    can_manage_memberships = bool(
        actor_user_id
        and business_id
        and memberships.can_manage_memberships(actor_user_id, business_id)
    )
    weekly_schedule = get_all_weekly_schedules_scoped(business_id) if business_id else []
    return render_template(
        "admin.html",
        appointments=get_appointments(status=status, appointment_date=appointment_date, business_id=business_id),
        counts=get_appointment_counts(business_id),
        selected_status=status,
        selected_date=appointment_date or "",
        business_name=settings["business_name"],
        business_initials=settings["business_initials"],
        business_settings=settings,
        weekly_schedule=weekly_schedule,
        config_message=request.args.get("config_message", ""),
        config_error=request.args.get("config_error", ""),
        services=services,
        resources=get_resources_scoped(business_id),
        resource_message=request.args.get("resource_message", ""),
        resource_error=request.args.get("resource_error", ""),
        onboarding=onboarding,
        product_summary=onboarding["summary"],
        service_message=request.args.get("service_message", ""),
        service_error=request.args.get("service_error", ""),
        reschedule_error=request.args.get("error_message", ""),
        schedule_message=request.args.get("schedule_message", ""),
        schedule_error=request.args.get("schedule_error", ""),
        can_manage_memberships=can_manage_memberships,
        smtp_configured=smtp_configured(),
    )


def _service_form_data(form):
    name = form.get("name", "").strip()
    price_raw = form.get("price", "").strip()
    duration_raw = form.get("duration", "").strip()
    active = form.get("active") == "1"

    if not name:
        return None, "El nombre del servicio es obligatorio."

    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return None, "El precio debe ser numérico y mayor o igual a cero."

    if not math.isfinite(price) or price < 0:
        return None, "El precio debe ser numérico y mayor o igual a cero."

    try:
        duration = int(duration_raw)
    except (TypeError, ValueError):
        return None, "La duración debe ser un entero mayor que cero."

    if duration <= 0:
        return None, "La duración debe ser un entero mayor que cero."

    return (name, price, duration, active), None


def _admin_url(**kwargs):
    """Devuelve la URL del panel admin, con prefijo de slug si aplica."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_slug", slug=business["slug"], **kwargs)
    return url_for("admin", **kwargs)


def _usuarios_url(**kwargs):
    """Devuelve la URL de la página de usuarios, con prefijo de slug si aplica."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_usuarios", slug=business["slug"], **kwargs)
    return url_for("admin_usuarios", **kwargs)


@app.route("/admin/servicios/guardar", methods=["POST"])
@app.route("/b/<slug>/admin/servicios/guardar", methods=["POST"])
def admin_save_service(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    values, error = _service_form_data(request.form)
    if error:
        return redirect(_admin_url(service_error=error))

    service_id = request.form.get("service_id", "").strip()
    try:
        if service_id:
            if not update_service_scoped(
                int(service_id), get_current_business_id(), *values
            ):
                return redirect(_admin_url(service_error="No se encontró el servicio."))
        else:
            create_service_scoped(get_current_business_id(), *values)
    except (TypeError, ValueError):
        return redirect(_admin_url(service_error="El identificador del servicio no es válido."))

    return redirect(_admin_url(service_message="El servicio se guardó correctamente."))


@app.route("/admin/servicios/<int:service_id>/estado", methods=["POST"])
@app.route("/b/<slug>/admin/servicios/<int:service_id>/estado", methods=["POST"])
def admin_toggle_service(service_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    active = request.form.get("active") == "1"
    business_id = get_current_business_id()
    service = next(
        (row for row in get_all_services_scoped(business_id) if row["id"] == service_id),
        None,
    )
    if not service:
        return redirect(_admin_url(service_error="No se encontró el servicio."))

    update_service_scoped(
        service_id,
        business_id,
        service["name"],
        service["price"],
        service["duration"],
        active,
    )
    return redirect(_admin_url(service_message="El estado del servicio se actualizó."))


@app.route("/admin/configuracion", methods=["POST"])
@app.route("/b/<slug>/admin/configuracion", methods=["POST"])
def admin_update_business_settings(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    business_name = request.form.get("business_name", "").strip()
    business_type = request.form.get("business_type", "").strip()
    business_initials = request.form.get("business_initials", "").strip()
    business_description = request.form.get("business_description", "").strip()
    timezone = request.form.get("timezone", "").strip()
    notifications_enabled = request.form.get("notifications_enabled") == "1"
    notification_email = request.form.get("notification_email", "").strip()
    slot_duration_raw = request.form.get("slot_duration", "").strip()
    break_between_slots_raw = request.form.get("break_between_slots", "").strip()

    if not business_name or not business_type or not business_initials:
        return redirect(_admin_url(
            config_error="Nombre, tipo e iniciales son obligatorios.",
        ))

    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return redirect(_admin_url(
            config_error="La zona horaria indicada no es válida.",
        ))

    slot_duration = None
    break_between_slots = None
    if slot_duration_raw:
        try:
            slot_duration = int(slot_duration_raw)
        except (TypeError, ValueError):
            return redirect(_admin_url(
                config_error="La duración de slot debe ser un número entero.",
            ))
        if slot_duration < 1:
            return redirect(_admin_url(
                config_error="La duración de slot debe ser al menos 1 minuto.",
            ))
    if break_between_slots_raw:
        try:
            break_between_slots = int(break_between_slots_raw)
        except (TypeError, ValueError):
            return redirect(_admin_url(
                config_error="El intervalo entre turnos debe ser un número entero.",
            ))
        if break_between_slots < 0:
            return redirect(_admin_url(
                config_error="El intervalo entre turnos no puede ser negativo.",
            ))

    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    update_business_settings_scoped(
        business_id,
        business_name,
        business_type,
        business_initials,
        business_description,
        timezone,
        notifications_enabled=notifications_enabled,
        notification_email=notification_email,
        slot_duration=slot_duration,
        break_between_slots=break_between_slots,
    )

    return redirect(_admin_url(
        config_message="La configuración del negocio se guardó correctamente.",
    ))


@app.route("/admin/horarios/guardar", methods=["POST"])
@app.route("/b/<slug>/admin/horarios/guardar", methods=["POST"])
def admin_save_weekly_schedule(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    for day in range(7):
        day_prefix = f"day_{day}"
        is_open = request.form.get(f"{day_prefix}_open") == "1"
        morning_start = request.form.get(f"{day_prefix}_morning_start", "").strip() or None
        morning_end = request.form.get(f"{day_prefix}_morning_end", "").strip() or None
        afternoon_start = request.form.get(f"{day_prefix}_afternoon_start", "").strip() or None
        afternoon_end = request.form.get(f"{day_prefix}_afternoon_end", "").strip() or None

        if not is_open:
            morning_start = None
            morning_end = None
            afternoon_start = None
            afternoon_end = None

        update_weekly_schedule_scoped(
            business_id, day, is_open, morning_start, morning_end,
            afternoon_start, afternoon_end,
        )

    return redirect(_admin_url(
        schedule_message="Los horarios semanales se guardaron correctamente.",
    ))
@app.route("/b/<slug>/admin/turnos/<int:appointment_id>/cancelar", methods=["POST"])
def admin_cancel_appointment(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    update_appointment_status_scoped(appointment_id, "cancelled", business_id)
    return redirect(_admin_url(status="confirmed"))


@app.route("/admin/turnos/<int:appointment_id>/estado", methods=["POST"])
@app.route("/b/<slug>/admin/turnos/<int:appointment_id>/estado", methods=["POST"])
def admin_update_appointment_status(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    status = request.form.get("status", "")
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return "Estado no válido", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    update_appointment_status_scoped(appointment_id, status, business_id)
    # Etapa 10.1: al marcar como completado, acreditar puntos de fidelización
    # (idempotente, no rompe el cambio de estado; "sticky award" en turnos ya
    # completados no duplica).
    if status == "completed":
        loyalty.award_points_for_completed(business_id, appointment_id)
    return redirect(_admin_url(status=status))


@app.route("/admin/turnos/<int:appointment_id>/reprogramar", methods=["POST"])
@app.route("/b/<slug>/admin/turnos/<int:appointment_id>/reprogramar", methods=["POST"])
def admin_reschedule_appointment(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    new_date = request.form.get("new_date", "").strip()
    new_time = request.form.get("new_time", "").strip()
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    error = None
    if not new_date or not new_time:
        error = "Completá la fecha y la hora para reprogramar."
    else:
        res = reschedule_appointment_admin(
            appointment_id, new_date, new_time, business_id=business_id
        )
        if not res["success"]:
            error = "No se pudo reprogramar el turno: " + _human_reschedule_error(res.get("reason"))

    return redirect(_admin_url(
        status="confirmed",
        error_message=error,
    ))


@app.route("/admin/turnos/<int:appointment_id>/reprogramar/con-recurso", methods=["POST"])
@app.route("/b/<slug>/admin/turnos/<int:appointment_id>/reprogramar/con-recurso", methods=["POST"])
def admin_reschedule_appointment_with_resource(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    new_date = request.form.get("new_date", "").strip()
    new_time = request.form.get("new_time", "").strip()
    resource_id_raw = request.form.get("resource_id", "").strip()
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    resource_id = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except (TypeError, ValueError):
            resource_id = None

    error = None
    if not new_date or not new_time:
        error = "Completá la fecha y la hora para reprogramar."
    else:
        res = reschedule_appointment_admin(
            appointment_id,
            new_date,
            new_time,
            business_id=business_id,
            resource_id=resource_id,
        )
        if not res["success"]:
            error = "No se pudo reprogramar el turno: " + _human_reschedule_error(res.get("reason"))

    return redirect(_admin_url(
        status="confirmed",
        error_message=error,
    ))


@app.route("/admin/turnos/crear", methods=["POST"])
@app.route("/b/<slug>/admin/turnos/crear", methods=["POST"])
def admin_create_appointment_manual(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    customer_name = request.form.get("customer_name", "").strip()
    phone = request.form.get("phone", "").strip()
    service_name = request.form.get("service_name", "").strip()
    appointment_date = request.form.get("appointment_date", "").strip()
    appointment_time = request.form.get("appointment_time", "").strip()
    notes = request.form.get("notes", "").strip()
    resource_id_raw = request.form.get("resource_id", "").strip()

    resource_id = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except (TypeError, ValueError):
            resource_id = None

    if not customer_name:
        return redirect(_admin_url(status="confirmed", error_message="El nombre del cliente es obligatorio."))
    if not phone:
        return redirect(_admin_url(status="confirmed", error_message="El teléfono del cliente es obligatorio."))
    if not service_name:
        return redirect(_admin_url(status="confirmed", error_message="El servicio es obligatorio."))
    if not appointment_date:
        return redirect(_admin_url(status="confirmed", error_message="La fecha es obligatoria."))
    if not appointment_time:
        return redirect(_admin_url(status="confirmed", error_message="El horario es obligatorio."))

    duration = None
    service = None
    for s in get_all_services_scoped(business_id):
        if s["name"] == service_name:
            service = s
            break
    if service is None:
        return redirect(_admin_url(status="confirmed", error_message="El servicio seleccionado no existe."))
    duration = service["duration"]

    # Validar recurso si se indica.
    if resource_id is not None:
        resource = get_resource_scoped(resource_id, business_id)
        if resource is None:
            return redirect(_admin_url(status="confirmed", error_message="El recurso seleccionado no existe o no pertenece a este negocio."))
        if not resource["active"]:
            return redirect(_admin_url(status="confirmed", error_message="No podés reservar un recurso inactivo."))

    res = create_appointment(
        customer_name=customer_name,
        phone=phone,
        service_name=service_name,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        status="confirmed",
        business_id=business_id,
        resource_id=resource_id,
        notes=notes or "",
    )
    if not res.get("success"):
        reason = res.get("reason") or "No se pudo crear la reserva."
        return redirect(_admin_url(
            status="confirmed",
            error_message="No se pudo crear la reserva: " + _human_appointment_error(reason),
        ))

    return redirect(_admin_url(
        status="confirmed",
        message=f"Turno creado: {customer_name} - {appointment_date} {appointment_time}",
    ))


def _human_appointment_error(reason):
    mapping = {
        "invalid_service": "El servicio indicado no es válido.",
        "invalid_time": "El horario indicado no es válido o ya no está disponible.",
        "occupied": "Ese horario ya está ocupado.",
        "invalid_date": "La fecha indicada no es válida.",
        "closed": "El negocio está cerrado en esa fecha.",
        "past": "No se puede reservar una fecha pasada.",
        "required_fields": "Faltan datos obligatorios.",
    }
    return mapping.get(reason, reason or "Operación no permitida.")


# ============================================================
# GESTIÓN DE USUARIOS / MEMBERSHIPS
# ============================================================

def _admin_usuarios_gate():
    """Autenticación administrativa para gestionar memberships.

    Solo valida sesión + membresía administrativa (owner/admin) del negocio
    actual. La política owner-only se delega a services.memberships para no
    duplicar reglas en la capa HTTP.
    """
    denied = _require_admin_membership()
    if denied:
        return denied
    return None


@app.route("/admin/usuarios")
@app.route("/b/<slug>/admin/usuarios")
def admin_usuarios(slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    result = memberships.list_members(actor_user_id, business_id)
    if not result["success"]:
        return redirect(_admin_url(usuarios_error=result["reason"]))
    settings = get_business_settings_scoped(business_id)
    business_name = (
        settings["business_name"] if settings and settings["business_name"] else "Mi negocio"
    )
    business_initials = (
        settings["business_initials"] if settings and settings["business_initials"] else ""
    )
    return render_template(
        "usuarios.html",
        business_name=business_name,
        business_initials=business_initials,
        members=result["members"],
        current_user_id=actor_user_id,
        message=request.args.get("usuarios_message", ""),
        error=request.args.get("usuarios_error", ""),
    )


@app.route("/admin/usuarios/invitar", methods=["POST"])
@app.route("/b/<slug>/admin/usuarios/invitar", methods=["POST"])
def admin_usuarios_invitar(slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    email = request.form.get("email", "").strip().lower()
    role_name = request.form.get("role_name", "").strip()

    if not email:
        return redirect(_usuarios_url(usuarios_error="El email es obligatorio."))

    user = get_user_by_email_scoped(email)
    if user is None:
        # Usuario nuevo: se crea sin contraseña útil (placeholder). El
        # flujo de asignación de contraseña/invitación por email queda
        # pendiente (fuera de alcance de esta fase).
        placeholder_hash = generate_password_hash(secrets.token_urlsafe(24))
        user_id = create_user_scoped(email, placeholder_hash, active=True)
        if user_id is None:
            return redirect(_usuarios_url(usuarios_error="No se pudo crear el usuario."))
        target_user_id = user_id
    else:
        target_user_id = user["id"]

    result = memberships.invite_member(
        actor_user_id, business_id, target_user_id, role_name
    )
    if not result["success"]:
        return redirect(_usuarios_url(usuarios_error=result["reason"]))
    return redirect(_usuarios_url(usuarios_message="Usuario agregado correctamente."))


@app.route("/admin/usuarios/<int:user_id>/rol", methods=["POST"])
@app.route("/b/<slug>/admin/usuarios/<int:user_id>/rol", methods=["POST"])
def admin_usuarios_cambiar_rol(user_id, slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    role_name = request.form.get("role_name", "").strip()
    result = memberships.change_role(
        actor_user_id, business_id, user_id, role_name
    )
    if not result["success"]:
        return redirect(_usuarios_url(usuarios_error=result["reason"]))
    return redirect(_usuarios_url(usuarios_message="Rol actualizado correctamente."))


@app.route("/admin/usuarios/<int:user_id>/revocar", methods=["POST"])
@app.route("/b/<slug>/admin/usuarios/<int:user_id>/revocar", methods=["POST"])
def admin_usuarios_revocar(user_id, slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    result = memberships.revoke_membership(actor_user_id, business_id, user_id)
    if not result["success"]:
        return redirect(_usuarios_url(usuarios_error=result["reason"]))
    return redirect(_usuarios_url(usuarios_message="Membresía revocada correctamente."))


# ============================================================
# FIDELIZACIÓN (MVP Etapa 10.1)
# ============================================================

def _fidelizacion_url(**kwargs):
    """URL de la página administrativa de fidelización, con/sin slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_fidelizacion_slug", slug=business["slug"], **kwargs)
    return url_for("admin_fidelizacion", **kwargs)


@app.route("/admin/fidelizacion")
@app.route("/b/<slug>/admin/fidelizacion")
def admin_fidelizacion(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    settings = get_business_settings_scoped(business_id)
    loyalty_settings = ensure_loyalty_settings_scoped(business_id)
    accounts = list_loyalty_accounts_scoped(business_id)
    selected_account_id = request.args.get("account_id", type=int)

    movements = []
    selected_account = None
    if selected_account_id is not None:
        selected_account = get_loyalty_account_by_id_scoped(selected_account_id, business_id)
        if selected_account is not None:
            movements = list_points_ledger_scoped(business_id, selected_account_id)

    business_name = settings["business_name"] if settings and settings["business_name"] else "Mi negocio"
    business_initials = settings["business_initials"] if settings and settings["business_initials"] else ""

    return render_template(
        "admin_fidelizacion.html",
        business_name=business_name,
        business_initials=business_initials,
        loyalty_settings=loyalty_settings,
        accounts=accounts,
        selected_account=selected_account,
        movements=movements,
        message=request.args.get("loyalty_message", ""),
        error=request.args.get("loyalty_error", ""),
        admin_prefix=("" if business_id == 1 else f"/b/{g.current_business['slug']}"),
    )


@app.route("/admin/fidelizacion/configuracion", methods=["POST"])
@app.route("/b/<slug>/admin/fidelizacion/configuracion", methods=["POST"])
def admin_fidelizacion_configuracion(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    enabled = request.form.get("enabled") == "1"
    points_raw = request.form.get("points_per_completed_appointment", "").strip()
    try:
        points = int(points_raw)
    except (TypeError, ValueError):
        return redirect(_fidelizacion_url(loyalty_error="El valor de puntos debe ser un número entero."))
    if points < 0:
        return redirect(_fidelizacion_url(loyalty_error="Los puntos por turno completado no pueden ser negativos."))

    result = update_loyalty_settings_scoped(business_id, enabled, points)
    if not result:
        return redirect(_fidelizacion_url(loyalty_error="No se pudo guardar la configuración de fidelización."))
    return redirect(_fidelizacion_url(
        loyalty_message="Configuración de fidelización guardada correctamente."
    ))


@app.route("/admin/fidelizacion/<int:account_id>/ajustar", methods=["POST"])
@app.route("/b/<slug>/admin/fidelizacion/<int:account_id>/ajustar", methods=["POST"])
def admin_fidelizacion_ajustar(account_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    actor_user_id = session.get("user_id")

    delta_raw = request.form.get("delta", "").strip()
    reason = request.form.get("reason", "").strip()
    result = loyalty.adjust_points(business_id, account_id, delta_raw, reason, actor_user_id)
    if not result["success"]:
        return redirect(_fidelizacion_url(
            loyalty_error="Ajuste rechazado: " + str(result["reason"]),
            account_id=account_id,
        ))
    return redirect(_fidelizacion_url(
        loyalty_message="Ajuste de puntos aplicado correctamente.",
        account_id=account_id,
    ))


@app.route("/admin/fidelizacion/recalcular", methods=["POST"])
@app.route("/b/<slug>/admin/fidelizacion/recalcular", methods=["POST"])
def admin_fidelizacion_recalcular(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    recalculate_all_balances_scoped(business_id)
    return redirect(_fidelizacion_url(loyalty_message="Saldo de todos los clientes recalculado."))


@app.route("/admin/fidelizacion/recompensas", methods=["GET", "POST"])
@app.route("/b/<slug>/admin/fidelizacion/recompensas", methods=["GET", "POST"])
def admin_recompensas(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return "Solicitud no válida", 400
        result = loyalty.save_reward(business_id, request.form.get("reward_id"), request.form.get("name"), request.form.get("description"), request.form.get("points_cost"), request.form.get("active") == "1")
        if not result["success"]:
            return redirect(_fidelizacion_url(loyalty_error="No se pudo guardar la recompensa."))
        return redirect(_fidelizacion_url(loyalty_message="Recompensa guardada."))
    connection = get_connection()
    try:
        redemptions = [dict(row) for row in connection.execute("""SELECT r.*, a.customer_name, a.customer_phone, w.name reward_name FROM redemptions r JOIN loyalty_accounts a ON a.id=r.account_id AND a.business_id=r.business_id JOIN rewards w ON w.id=r.reward_id AND w.business_id=r.business_id WHERE r.business_id=? ORDER BY r.id DESC""", (business_id,)).fetchall()]
    finally:
        connection.close()
    reward_id = request.args.get("edit", type=int)
    editing = next((reward for reward in loyalty.list_rewards(business_id) if reward["id"] == reward_id), None)
    loyalty_enabled = bool(ensure_loyalty_settings_scoped(business_id).get("enabled"))
    return render_template("admin_recompensas.html", rewards=loyalty.list_rewards(business_id), retention=loyalty.retention_candidates(business_id) if loyalty_enabled else [], redemptions=redemptions, editing=editing, loyalty_enabled=loyalty_enabled)


@app.route("/admin/fidelizacion/recompensas/<int:reward_id>/estado", methods=["POST"])
@app.route("/b/<slug>/admin/fidelizacion/recompensas/<int:reward_id>/estado", methods=["POST"])
def admin_recompensa_estado(reward_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    reward = next((r for r in loyalty.list_rewards(get_current_business_id()) if r["id"] == reward_id), None)
    if reward is None:
        abort(404)
    loyalty.save_reward(get_current_business_id(), reward_id, reward["name"], reward["description"], reward["points_cost"], request.form.get("active") == "1")
    return redirect(_fidelizacion_url())
# ============================================================
# CHAT IA
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "El formato enviado no es válido."}), 400

        if not is_chat_request_allowed(get_client_ip(), get_current_business_id()):
            return jsonify({
                "success": False,
                "error": "Esperá un momento antes de enviar otro mensaje."
            }), 429

        raw_message = data.get("message", "")
        if not isinstance(raw_message, str):
            return jsonify({"success": False, "error": "El mensaje debe ser texto."}), 400

        message = raw_message.strip()

        conversation = data.get(
            "conversation",
            []
        )

        if not isinstance(conversation, list) or len(conversation) > MAX_HISTORY_MESSAGES:
            return jsonify({"success": False, "error": "El historial no es válido."}), 400

        if any(
            not isinstance(item, dict)
            or item.get("role") not in ("user", "assistant")
            or not isinstance(item.get("content"), str)
            or len(item["content"]) > MAX_HISTORY_CONTENT_LENGTH
            for item in conversation
        ):
            return jsonify({"success": False, "error": "El historial no es válido."}), 400

        if not message:

            return jsonify({
                "success": False,
                "error": "No se recibió ningún mensaje."
            }), 400

        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({
                "success": False,
                "error": "El mensaje es demasiado largo."
            }), 400

        # Datos opcionales del cliente para persistencia de conversación
        customer_phone = data.get("customer_phone", "").strip() if isinstance(data.get("customer_phone"), str) else ""
        customer_name = data.get("customer_name", "").strip() if isinstance(data.get("customer_name"), str) else ""
        customer_email = data.get("customer_email", "").strip() if isinstance(data.get("customer_email"), str) else ""


        response = ask_ai(
            message,
            conversation,
            business_id=get_current_business_id(),
            customer_phone=customer_phone,
            customer_name=customer_name,
            customer_email=customer_email,
        )


        return jsonify({
            "success": True,
            "response": response
        })


    except Exception as error:

        logger.exception("Error procesando /chat")

        return jsonify({
            "success": False,
            "error": "No se pudo procesar la consulta."
        }), 500


# ============================================================
# API - SERVICIOS
# ============================================================

def _get_public_services_response(business_id):
    if not is_api_request_allowed(get_client_ip(), "api:servicios", business_id):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429
    if business_id is None:
        return jsonify({
            "success": False,
            "error": "No hay un negocio activo para esta solicitud."
        }), 404

    services = get_active_services_scoped(business_id)
    servicios = []
    for row in services:
        servicios.append({
            "nombre": row["name"],
            "precio": row["price"],
            "duracion": row["duration"]
        })

    return jsonify({
        "success": True,
        "servicios": servicios
    })


@app.route("/api/servicios", methods=["GET"])
def api_servicios():
    return _get_public_services_response(get_current_business_id())


@app.route("/b/<slug>/api/servicios", methods=["GET"])
def business_api_servicios(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    return _get_public_services_response(get_current_business_id())


# ============================================================
# API - RECURSOS (público)
# ============================================================

def _get_public_resources_response(business_id):
    """Devuelve los recursos activos del negocio actual."""
    if not is_api_request_allowed(get_client_ip(), "api:recursos", business_id):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429
    if business_id is None:
        return jsonify({"success": False, "error": "No hay un negocio activo para esta solicitud."}), 404

    resources = get_resources_scoped(business_id, only_active=True)
    recursos = []
    for row in resources:
        recursos.append({
            "id": row["id"],
            "nombre": row["name"]
        })

    return jsonify({
        "success": True,
        "recursos": recursos
    })


@app.route("/api/recursos", methods=["GET"])
def api_recursos():
    return _get_public_resources_response(get_current_business_id())


@app.route("/b/<slug>/api/recursos", methods=["GET"])
def business_api_recursos(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    return _get_public_resources_response(get_current_business_id())


# ============================================================
# API - PUNTOS DE FIDELIZACIÓN (cliente)
# ============================================================

def _get_public_points_response(business_id):
    """Saldo de puntos del cliente para una consulta pública del negocio.

    Solo devuelve el saldo si fidelización está habilitada y el phone es válido.
    No expone el ledger completo (es info sensible; en v1 el cliente ve el saldo,
    el historial detallado queda para el panel admin).
    """
    if not is_api_request_allowed(get_client_ip(), "api:puntos", business_id):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429
    if business_id is None:
        return jsonify({"success": False, "error": "No hay un negocio activo para esta solicitud."}), 404

    settings = ensure_loyalty_settings_scoped(business_id)
    if not settings or not settings.get("enabled"):
        return jsonify({"success": True, "enabled": False, "balance": 0})

    phone = (request.args.get("phone") or "").strip()
    account = loyalty.get_account(business_id, phone) if phone else None
    return jsonify({
        "success": True,
        "enabled": True,
        "points_per_completed": settings.get("points_per_completed_appointment"),
        "balance": account["points_balance"] if account else 0,
    })


@app.route("/api/puntos", methods=["GET"])
def api_puntos():
    return _get_public_points_response(get_current_business_id())


@app.route("/b/<slug>/api/puntos", methods=["GET"])
def business_api_puntos(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)
    g.current_business = business
    return _get_public_points_response(get_current_business_id())

# ============================================================
# API - DISPONIBILIDAD
# ============================================================

def _get_public_availability_response(fecha, business_id):
    if not is_api_request_allowed(get_client_ip(), "api:disponibilidad", business_id):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429

    # resource_id opcional: permite filtrar disponibilidad por recurso
    resource_id_raw = request.args.get("resource_id")
    resource_id = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "resource_id inválido."}), 400

    try:
        horarios = get_available_times(fecha, business_id, request.args.get("servicio"), resource_id)
        return jsonify({
            "success": True,
            "fecha": fecha,
            "horarios_disponibles": horarios
        })
    except Exception:
        logger.exception("Error consultando disponibilidad")
        return jsonify({
            "success": False,
            "error": "No se pudo consultar la disponibilidad."
        }), 500


@app.route(
    "/api/disponibilidad/<fecha>",
    methods=["GET"]
)
def api_disponibilidad(fecha):
    return _get_public_availability_response(fecha, get_current_business_id())


@app.route(
    "/b/<slug>/api/disponibilidad/<fecha>",
    methods=["GET"]
)
def business_api_disponibilidad(slug, fecha):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    return _get_public_availability_response(fecha, get_current_business_id())


# ============================================================
# API - BUSCAR TURNOS
# ============================================================

def _get_public_appointments_response(business_id):
    if not is_api_request_allowed(get_client_ip(), "api:turnos", business_id):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429

    nombre = request.args.get(
        "nombre",
        ""
    ).strip()

    telefono = request.args.get("telefono", "").strip()


    if not nombre:

        return jsonify({
            "success": False,
            "error": "El nombre es obligatorio."
        }), 400

    if not telefono:
        return jsonify({
            "success": False,
            "error": "El teléfono es obligatorio para consultar tus turnos."
        }), 400


    try:

        turnos = get_customer_appointments(
            nombre,
            telefono,
            business_id,
        )


        return jsonify({
            "success": True,
            "turnos": turnos
        })


    except Exception:

        logger.exception("Error buscando turnos")

        return jsonify({
            "success": False,
            "error": "No se pudieron consultar los turnos."
        }), 500


@app.route(
    "/api/turnos",
    methods=["GET"]
)
def api_turnos():
    return _get_public_appointments_response(get_current_business_id())


@app.route(
    "/b/<slug>/api/turnos",
    methods=["GET"]
)
def business_api_turnos(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _get_public_appointments_response(business_id)


# ============================================================
# API - RESERVAR
# ============================================================

def _create_public_appointment_response(business_id):

    if not is_api_request_allowed(get_client_ip(), "api:reservar", business_id):


        return jsonify({
            "success": False,
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        nombre = data.get("nombre", "")
        telefono = data.get("telefono", "")
        if not isinstance(nombre, str) or not isinstance(telefono, str):
            return jsonify({"success": False, "error": "Nombre y teléfono deben ser texto."}), 400
        nombre = nombre.strip()

        telefono = telefono.strip()

        email = data.get("email", "")
        if not isinstance(email, str):
            email = ""
        email = email.strip()

        servicio = data.get(
            "servicio"
        )

        fecha = data.get(
            "fecha"
        )

        hora = data.get(
            "hora"
        )


        # ----------------------------------------------------
        # VALIDACIONES
        # ----------------------------------------------------

        if not nombre:

            return jsonify({
                "success": False,
                "error": "El nombre y apellido son obligatorios."
            }), 400


        if not telefono:

            return jsonify({
                "success": False,
                "error": "El teléfono es obligatorio."
            }), 400


        if email and not validate_email(email):

            return jsonify({
                "success": False,
                "error": "El email no es válido."
            }), 400


        if not email and notifications_enabled(business_id):

            return jsonify({
                "success": False,
                "reason": "email_required",
                "error": "El email es obligatorio para poder enviarte la confirmación del turno."
            }), 400


        services = get_active_services_scoped(business_id) if business_id is not None else []
        allowed_services = {row["name"] for row in services}

        if servicio not in allowed_services:

            return jsonify({
                "success": False,
                "error": "El servicio seleccionado no es válido."
            }), 400


        if not fecha or not hora:

            return jsonify({
                "success": False,
                "error": "La fecha y el horario son obligatorios."
            }), 400


        # ----------------------------------------------------
        # VALIDAR RECURSO (opcional)
        # ----------------------------------------------------

        resource_id_raw = data.get("resource_id")
        resource_id = None
        if resource_id_raw is not None:
            try:
                resource_id = int(resource_id_raw)
                if resource_id <= 0:
                    resource_id = None
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "resource_id inválido."}), 400


        # ----------------------------------------------------
        # CREAR TURNO
        # ----------------------------------------------------

        resultado = create_appointment(

            customer_name=nombre,

            phone=telefono,

            service=servicio,

            appointment_date=fecha,

            appointment_time=hora,

            business_id=business_id,

            email=email or None,

            resource_id=resource_id,
        )


        # ----------------------------------------------------
        # FECHA PASADA
        # ----------------------------------------------------

        if resultado.get("reason") == "past_date":

            return jsonify({
                "success": False,
                "reason": "past_date",
                "error": "No podés reservar una fecha que ya pasó."
            }), 400


        # ----------------------------------------------------
        # DÍA CERRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "closed_day":

            return jsonify({
                "success": False,
                "reason": "closed_day",
                "error": "Ese día estamos cerrados."
            }), 400


        # ----------------------------------------------------
        # FECHA INVÁLIDA
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_date":

            return jsonify({
                "success": False,
                "reason": "invalid_date",
                "error": "La fecha seleccionada no es válida."
            }), 400


        # ----------------------------------------------------
        # HORARIO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_time":

            return jsonify({
                "success": False,
                "reason": "invalid_time",
                "error": "El horario seleccionado no es válido."
            }), 400


        # ----------------------------------------------------
        # HORARIO OCUPADO
        # ----------------------------------------------------

        if resultado.get("reason") == "occupied":

            return jsonify({
                "success": False,
                "reason": "occupied",
                "error": "Ese horario ya está ocupado."
            }), 400


        # ----------------------------------------------------
        # RESERVA CORRECTA
        # ----------------------------------------------------

        if resultado.get("success"):

            # ----------------------------------------------------
            # CONFIRMACIÓN POR EMAIL (no bloqueante para la reserva)
            # ----------------------------------------------------

            try:
                current = getattr(g, "current_business", None) or {}
                slug = current.get("slug") or None
                base_url = request.url_root.rstrip("/")
                appointment_data = {
                    "id": resultado.get("appointment_id"),
                    "customer_name": resultado.get("customer_name"),
                    "customer_email": resultado.get("customer_email"),
                    "service": resultado.get("service"),
                    "appointment_date": resultado.get("appointment_date"),
                    "appointment_time": resultado.get("appointment_time"),
                    "appointment_end": resultado.get("appointment_end"),
                }
                send_confirmation_email(
                    business_id,
                    appointment_data,
                    management_token=resultado.get("management_token"),
                    slug=slug,
                    public_base_url=base_url,
                )
                # EMAIL 5: aviso al negocio (scoped, no bloqueante)
                send_business_confirmation_email(business_id, appointment_data)
            except Exception:
                logger.exception("Error enviando confirmación de turno")

            response_data = {
                "success": True,
                "appointment_id": resultado.get("appointment_id"),
                "management_token": resultado.get("management_token"),
                "message": "El turno fue reservado correctamente."
            }
            if resultado.get("resource_id"):
                response_data["resource_id"] = resultado["resource_id"]
                # Obtener el nombre del recurso para la respuesta
                resource = get_resource_scoped(resultado["resource_id"], business_id)
                if resource:
                    response_data["resource_nombre"] = resource["name"]

            return jsonify(response_data), 201

        if resultado.get("reason") == "past_time":
            return jsonify({
                "success": False,
                "reason": "past_time",
                "error": "Ese horario ya pasó.",
            }), 400

        if resultado.get("reason") == "invalid_service":
            return jsonify({
                "success": False,
                "reason": "invalid_service",
                "error": "El servicio seleccionado no está disponible.",
            }), 400

        if resultado.get("reason") == "invalid_resource":
            return jsonify({
                "success": False,
                "reason": "invalid_resource",
                "error": "El recurso seleccionado no es válido o no está disponible.",
            }), 400


        # ----------------------------------------------------
        # ERROR DESCONOCIDO
        # ----------------------------------------------------

        return jsonify({
            "success": False,
            "error": "No se pudo realizar la reserva."
        }), 500


    except Exception as error:

        logger.exception("Error reservando turno")

        return jsonify({
            "success": False,
            "error": "No se pudo realizar la reserva."
        }), 500


# ============================================================
# RECURSOS (reservables, multi-tenant, scoped por business_id)
# ============================================================


@app.route("/admin/recursos")
@app.route("/b/<slug>/admin/recursos")
def admin_recursos(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    return _render_admin()


@app.route("/admin/recursos/crear", methods=["POST"])
@app.route("/b/<slug>/admin/recursos/crear", methods=["POST"])
def admin_create_resource(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(_admin_url(resource_error="El nombre del recurso es obligatorio."))
    if len(name) > 120:
        return redirect(_admin_url(resource_error="El nombre del recurso es demasiado largo."))
    result = create_resource_scoped(business_id, name)
    if not result.get("success"):
        if result.get("reason") == "duplicate_name":
            return redirect(_admin_url(resource_error="Ya existe un recurso con ese nombre en este negocio."))
        return redirect(_admin_url(resource_error="No se pudo crear el recurso."))
    return redirect(_admin_url(resource_message="El recurso se creó correctamente."))


@app.route("/admin/recursos/<int:resource_id>/estado", methods=["POST"])
@app.route("/b/<slug>/admin/recursos/<int:resource_id>/estado", methods=["POST"])
def admin_toggle_resource(resource_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    resource = get_resource_scoped(resource_id, business_id)
    if resource is None:
        return redirect(_admin_url(resource_error="No se encontró el recurso."))
    new_active = request.form.get("active") == "1"
    set_resource_active_scoped(resource_id, business_id, new_active)
    return redirect(_admin_url(resource_message="El estado del recurso se actualizó."))


def _resources_url(**kwargs):
    """URL de la página de recursos, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_recursos_slug", slug=business["slug"], **kwargs)
    return url_for("admin_recursos", **kwargs)


def _conocimiento_url(**kwargs):
    """URL de la página de conocimiento, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_conocimiento_slug", slug=business["slug"], **kwargs)
    return url_for("admin_conocimiento", **kwargs)


@app.route("/admin/conocimiento")
@app.route("/b/<slug>/admin/conocimiento")
def admin_conocimiento(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    knowledge = get_knowledge_scoped(business_id)
    settings = get_business_settings_scoped(business_id)
    business_name = settings["business_name"] if settings else "Mi negocio"
    business_initials = settings["business_initials"] if settings else ""
    return render_template(
        "admin_conocimiento.html",
        business_name=business_name,
        business_initials=business_initials,
        knowledge=knowledge,
        message=request.args.get("conocimiento_message", ""),
        error=request.args.get("conocimiento_error", ""),
    )


@app.route("/admin/conocimiento/crear", methods=["POST"])
@app.route("/b/<slug>/admin/conocimiento/crear", methods=["POST"])
def admin_conocimiento_crear(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    type_ = request.form.get("type", "").strip()
    question = request.form.get("question", "").strip()
    answer = request.form.get("answer", "").strip()
    tags = request.form.get("tags", "").strip()
    if not type_ or not question or not answer:
        return redirect(_conocimiento_url(conocimiento_error="Tipo, pregunta y respuesta son obligatorios."))
    actor_user_id = session.get("user_id")
    create_knowledge_scoped(business_id, type_, question, answer, tags, actor_user_id)
    return redirect(_conocimiento_url(conocimiento_message="Entrada de conocimiento creada correctamente."))


@app.route("/admin/conocimiento/<int:knowledge_id>/editar", methods=["POST"])
@app.route("/b/<slug>/admin/conocimiento/<int:knowledge_id>/editar", methods=["POST"])
def admin_conocimiento_editar(knowledge_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    type_ = request.form.get("type", "").strip()
    question = request.form.get("question", "").strip()
    answer = request.form.get("answer", "").strip()
    tags = request.form.get("tags", "").strip()
    active = request.form.get("active") == "1"
    if not type_ or not question or not answer:
        return redirect(_conocimiento_url(conocimiento_error="Tipo, pregunta y respuesta son obligatorios."))
    if not update_knowledge_scoped(knowledge_id, business_id, type_, question, answer, tags, active):
        return redirect(_conocimiento_url(conocimiento_error="No se pudo actualizar la entrada."))
    return redirect(_conocimiento_url(conocimiento_message="Entrada de conocimiento actualizada correctamente."))


@app.route("/admin/conocimiento/<int:knowledge_id>/eliminar", methods=["POST"])
@app.route("/b/<slug>/admin/conocimiento/<int:knowledge_id>/eliminar", methods=["POST"])
def admin_conocimiento_eliminar(knowledge_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    if not delete_knowledge_scoped(knowledge_id, business_id):
        return redirect(_conocimiento_url(conocimiento_error="No se pudo eliminar la entrada."))
    return redirect(_conocimiento_url(conocimiento_message="Entrada de conocimiento eliminada correctamente."))


def _conversaciones_url(**kwargs):
    """URL de la página de conversaciones, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_conversaciones_slug", slug=business["slug"], **kwargs)
    return url_for("admin_conversaciones", **kwargs)


@app.route("/admin/conversaciones")
@app.route("/b/<slug>/admin/conversaciones")
def admin_conversaciones(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    status_filter = request.args.get("status", "all")
    if status_filter not in ("all", "active", "needs_human", "human_resolved", "closed"):
        status_filter = "all"

    status_filter_db = None if status_filter == "all" else status_filter
    conversations = list_conversation_sessions_scoped(business_id, status=status_filter_db, limit=100)

    settings = get_business_settings_scoped(business_id)
    business_name = settings["business_name"] if settings else "Mi negocio"
    business_initials = settings["business_initials"] if settings else ""

    stats = get_conversation_stats_scoped(business_id)

    return render_template(
        "admin_conversaciones.html",
        business_name=business_name,
        business_initials=business_initials,
        conversations=conversations,
        stats=stats,
        status_filter=status_filter,
        message=request.args.get("conversaciones_message", ""),
        error=request.args.get("conversaciones_error", ""),
    )


@app.route("/admin/conversaciones/<int:session_id>")
@app.route("/b/<slug>/admin/conversaciones/<int:session_id>")
def admin_conversaciones_detalle(session_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    session = get_conversation_session_by_id_scoped(session_id, business_id)
    if not session:
        return redirect(_conversaciones_url(conversaciones_error="Conversación no encontrada."))

    messages = get_conversation_messages_scoped(session_id, business_id)

    settings = get_business_settings_scoped(business_id)
    business_name = settings["business_name"] if settings else "Mi negocio"
    business_initials = settings["business_initials"] if settings else ""

    return render_template(
        "admin_conversaciones_detalle.html",
        business_name=business_name,
        business_initials=business_initials,
        session=session,
        messages=messages,
        message=request.args.get("conversaciones_message", ""),
        error=request.args.get("conversaciones_error", ""),
    )


@app.route("/admin/conversaciones/<int:session_id>/resolver", methods=["POST"])
@app.route("/b/<slug>/admin/conversaciones/<int:session_id>/resolver", methods=["POST"])
def admin_conversaciones_resolver(session_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    if not resolve_human_handoff_scoped(session_id, business_id):
        return redirect(_conversaciones_url(conversaciones_error="No se pudo resolver la conversación."))

    return redirect(_conversaciones_url(conversaciones_message="Conversación marcada como resuelta."))


@app.route("/admin/conversaciones/<int:session_id>/estado", methods=["POST"])
@app.route("/b/<slug>/admin/conversaciones/<int:session_id>/estado", methods=["POST"])
def admin_conversaciones_estado(session_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    new_status = request.form.get("status", "").strip()
    if new_status not in ("active", "needs_human", "human_resolved", "closed"):
        return redirect(_conversaciones_url(conversaciones_error="Estado inválido."))

    if not update_session_status_scoped(session_id, business_id, new_status):
        return redirect(_conversaciones_url(conversaciones_error="No se pudo actualizar el estado."))

    return redirect(_conversaciones_url(conversaciones_message="Estado actualizado correctamente."))


# ============================================================
# ANALYTICS: INTELIGENCIA
# ============================================================

@app.route("/admin/inteligencia")
@app.route("/b/<slug>/admin/inteligencia")
def admin_inteligencia(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    frequent_questions = get_frequent_questions_scoped(business_id, limit=50)
    unanswered_questions = get_unanswered_questions_scoped(business_id, limit=50)
    opportunities = get_opportunities_scoped(business_id, limit=20)
    stats = get_conversation_stats_scoped(business_id)

    settings = get_business_settings_scoped(business_id)
    business_name = settings["business_name"] if settings else "Mi negocio"
    business_initials = settings["business_initials"] if settings else ""

    return render_template(
        "admin_inteligencia.html",
        business_name=business_name,
        business_initials=business_initials,
        frequent_questions=frequent_questions,
        unanswered_questions=unanswered_questions,
        opportunities=opportunities,
        stats=stats,
        message=request.args.get("inteligencia_message", ""),
        error=request.args.get("inteligencia_error", ""),
    )


@app.route("/admin/inteligencia/oportunidades")
@app.route("/b/<slug>/admin/inteligencia/oportunidades")
def admin_inteligencia_oportunidades(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    opportunities = get_opportunities_scoped(business_id, limit=50)
    stats = get_conversation_stats_scoped(business_id)

    settings = get_business_settings_scoped(business_id)
    business_name = settings["business_name"] if settings else "Mi negocio"
    business_initials = settings["business_initials"] if settings else ""

    return render_template(
        "admin_inteligencia_oportunidades.html",
        business_name=business_name,
        business_initials=business_initials,
        opportunities=opportunities,
        stats=stats,
        message=request.args.get("inteligencia_message", ""),
        error=request.args.get("inteligencia_error", ""),
    )


@app.route(
    "/api/reservar",
    methods=["POST"]
)
def api_reservar():
    return _create_public_appointment_response(get_current_business_id())


@app.route(
    "/b/<slug>/api/reservar",
    methods=["POST"]
)
def business_api_reservar(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _create_public_appointment_response(business_id)


# ============================================================
# PÁGINA PÚBLICA - GESTIONAR TURNO (enlace seguro con management_token)
# ============================================================

@app.route("/b/<slug>/turno/<token>", methods=["GET", "POST"])
def public_manage_turno(slug, token):
    business = resolve_business(slug)
    if business is None or business.get("slug") != slug:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    appointment_id = request.args.get("id") or (request.form.get("id") or request.view_args.get("id"))
    try:
        appointment_id = int(appointment_id)
    except (TypeError, ValueError):
        appointment_id = None

    if appointment_id is None or appointment_id <= 0:
        return render_template(
            "gestionar_turno.html",
            business=business,
            appointment=None,
            error="El enlace no es válido o el turno no existe.",
            message=None,
        ), 404

    appointment = get_appointment_by_token(appointment_id, business_id, token)
    if appointment is not None:
        appointment = dict(appointment)
    if appointment is None:
        return render_template(
            "gestionar_turno.html",
            business=business,
            appointment=None,
            error="El enlace no es válido o el turno no existe.",
            message=None,
        ), 404

    error = None
    message = None

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "cancelar":
            if cancel_appointment(
                appointment_id,
                appointment.get("phone") or "",
                business_id,
                management_token=token,
            ):
                message = "Tu turno fue cancelado correctamente."
                appointment = get_appointment_by_token(appointment_id, business_id, token)
                if appointment is not None:
                    appointment = dict(appointment)
            else:
                error = "No se pudo cancelar el turno. Puede que ya haya sido cancelado."

        elif action == "reprogramar":
            nueva_fecha = (request.form.get("fecha") or "").strip()
            nueva_hora = (request.form.get("hora") or "").strip()
            res = reschedule_appointment(
                appointment_id,
                nueva_fecha,
                nueva_hora,
                appointment.get("phone") or "",
                business_id,
                management_token=token,
            )
            if res.get("success"):
                message = "Tu turno fue reprogramado correctamente."
                appointment = get_appointment_by_token(appointment_id, business_id, token)
                if appointment is not None:
                    appointment = dict(appointment)
            else:
                error = "No se pudo reprogramar el turno: " + _human_reschedule_error(res.get("reason"))

        elif action == "canjear":
            settings = ensure_loyalty_settings_scoped(business_id)
            result = loyalty.redeem(
                business_id,
                loyalty.get_account(business_id, appointment.get("phone") or "") ["id"],
                request.form.get("reward_id"),
                request.form.get("idempotency_key"),
            ) if settings and settings.get("enabled") and loyalty.get_account(business_id, appointment.get("phone") or "") else {"success": False, "reason": "disabled"}
            message = "Canje realizado correctamente." if result["success"] else "No se pudo realizar el canje: " + result["reason"]
            if not result["success"]:
                error, message = message, None

    return render_template(
        "gestionar_turno.html",
        business=business,
        appointment=appointment,
        error=error,
        message=message,
        loyalty=_loyalty_public_context(business_id, appointment),
    )


def _loyalty_public_context(business_id, appointment):
    """Contexto público de fidelización para la vista del cliente.

    Devuelve un dict que la plantilla usa para mostrar el bloque "Mis puntos".
    Si fidelización está desactivada o no hay cuenta, devuelve un estado minimal
    para no inventar puntos de un sistema apagado.
    """
    if appointment is None:
        return {"enabled": False, "balance": 0, "settings": None}
    settings = ensure_loyalty_settings_scoped(business_id)
    enabled = bool(settings and settings.get("enabled"))
    if not enabled:
        return {"enabled": False, "balance": 0, "settings": settings}
    phone = (appointment.get("phone") or "").strip()
    balance = loyalty.get_balance(business_id, phone) if phone else 0
    return {
        "enabled": True,
        "balance": balance,
        "points_per_completed": settings.get("points_per_completed_appointment") if settings else 0,
        "rewards": loyalty.list_rewards(business_id, active_only=True),
        "idempotency_key": secrets.token_urlsafe(24),
    }


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
# API - CANCELAR
# ============================================================

def _cancel_public_appointment_response(business_id):
    if not is_api_request_allowed(get_client_ip(), "api:cancelar", business_id):
        return jsonify({
            "success": False,
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        appointment_id = data.get(
            "appointment_id"
        )

        telefono = data.get("telefono", "").strip()


        if not appointment_id:

            return jsonify({
                "success": False,
                "error": "Falta el ID del turno."
            }), 400

        management_token = data.get("management_token")
        if management_token is not None and (not isinstance(management_token, str) or not management_token):
            return jsonify({"success": False, "error": "No pudimos validar ese turno."}), 400

        telefono = data.get("telefono", "").strip()
        customer_name = data.get("customer_name", "").strip()

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400

        if not management_token and not customer_name:
            return jsonify({"success": False, "error": "El nombre del cliente es obligatorio."}), 400


        resultado = cancel_appointment(
            appointment_id,
            telefono,
            business_id,
            management_token,
            customer_name if customer_name else None,
        )


        if not resultado:

            return jsonify({
                "success": False,
                "error": "No pudimos encontrar ese turno con los datos indicados. Verificá tu nombre y teléfono e intentá nuevamente."
            }), 400


        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue cancelado correctamente."
        })


    except Exception:

        logger.exception("Error cancelando turno")

        return jsonify({
            "success": False,
            "error": "No se pudo cancelar el turno."
        }), 500


@app.route(
    "/api/cancelar",
    methods=["POST"]
)
def api_cancelar():
    return _cancel_public_appointment_response(get_current_business_id())


@app.route(
    "/b/<slug>/api/cancelar",
    methods=["POST"]
)
def business_api_cancelar(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _cancel_public_appointment_response(business_id)


# ============================================================
# API - REPROGRAMAR
# ============================================================

def _get_public_reschedule_response(business_id):

    if not is_api_request_allowed(get_client_ip(), "api:reprogramar", business_id):
        return jsonify({
            "success": False,
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400


        appointment_id = data.get(
            "appointment_id"
        )

        nueva_fecha = data.get(
            "nueva_fecha"
        )

        nueva_hora = data.get(
            "nueva_hora"
        )

        telefono = data.get("telefono", "").strip()


        if not appointment_id:

            return jsonify({
                "success": False,
                "error": "Falta el ID del turno."
            }), 400

        management_token = data.get("management_token")
        if management_token is not None and (not isinstance(management_token, str) or not management_token):
            return jsonify({"success": False, "error": "No pudimos validar ese turno."}), 400

        telefono = data.get("telefono", "").strip()
        customer_name = data.get("customer_name", "").strip()

        if not nueva_fecha or not nueva_hora:
            return jsonify({
                "success": False,
                "error": "La nueva fecha y hora son obligatorias."
            }), 400

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400

        if not management_token and not customer_name:
            return jsonify({"success": False, "error": "El nombre del cliente es obligatorio."}), 400


        resultado = reschedule_appointment(

            appointment_id,

            nueva_fecha,

            nueva_hora,

            telefono,

            business_id,
            management_token,
            customer_name if customer_name else None,
        )


        # ----------------------------------------------------
        # HORARIO OCUPADO
        # ----------------------------------------------------

        if resultado.get("reason") == "occupied":

            return jsonify({

                "success": False,

                "reason": "occupied",

                "error": "El nuevo horario ya está ocupado."
            })


        # ----------------------------------------------------
        # TURNO NO ENCONTRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "not_found":

            return jsonify({

                "success": False,

                "reason": "not_found",

                "error": "No pudimos encontrar ese turno con los datos indicados. Verificá tu nombre y teléfono e intentá nuevamente."
            })


        # ----------------------------------------------------
        # HORARIO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_time":

            return jsonify({

                "success": False,

                "reason": "invalid_time",

                "error": "El horario seleccionado no es válido."
            }), 400

        if resultado.get("reason") == "past_time":
            return jsonify({
                "success": False,
                "reason": "past_time",
                "error": "Ese horario ya pasó.",
            }), 400
                # ----------------------------------------------------
        # FECHA PASADA
        # ----------------------------------------------------

        if resultado.get("reason") == "past_date":

            return jsonify({

                "success": False,

                "reason": "past_date",

                "error": "No podés reprogramar el turno para una fecha que ya pasó."
            }), 400


        # ----------------------------------------------------
        # DÍA CERRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "closed_day":

            return jsonify({

                "success": False,

                "reason": "closed_day",

                "error": "Ese día estamos cerrados."
            }), 400


        # ----------------------------------------------------
        # FECHA INVÁLIDA
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_date":

            return jsonify({

                "success": False,

                "reason": "invalid_date",

                "error": "La fecha seleccionada no es válida."
            }), 400


        # ----------------------------------------------------
        # CORRECTO
        # ----------------------------------------------------

        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue reprogramado correctamente."
        })


    except Exception as error:

        logger.exception("Error reprogramando turno")

        return jsonify({

            "success": False,

            "error": "No se pudo reprogramar el turno."
        }), 500


@app.route(
    "/api/reprogramar",
    methods=["POST"]
)
def api_reprogramar():
    return _get_public_reschedule_response(get_current_business_id())


@app.route(
    "/b/<slug>/api/reprogramar",
    methods=["POST"]
)
def business_api_reprogramar(slug):
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _get_public_reschedule_response(business_id)


# ============================================================
# INICIAR SERVIDOR
# ============================================================

if __name__ == "__main__":

    app.run(

        debug=False,

        host="127.0.0.1",

        port=5000
    )
