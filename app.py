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
from database.database import (
    get_business_settings,
    get_business_settings_scoped,
    get_connection,
    init_database,
    update_appointment_status,
    update_appointment_status_scoped,
    update_business_settings,
    update_business_settings_scoped,
    get_all_services,
    create_service,
    update_service,
    get_active_services_scoped,
    get_all_services_scoped,
    create_service_scoped,
    update_service_scoped,
)

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
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
                "SELECT id, name, slug FROM businesses WHERE slug = ?",
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
        slug = request.view_args.get("slug")
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
    """Crea una sesión persistente tras un login exitoso y regenera la sesión HTTP."""
    old_csrf = session.get("csrf_token")
    session.clear()
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
    session.clear()
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
    session.clear()
    return redirect(url_for("login_slug", slug=business["slug"]))


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
        }
    status = request.args.get("status") or "confirmed"
    if status not in {"confirmed", "cancelled"}:
        status = "confirmed"
    appointment_date = request.args.get("fecha") or None
    actor_user_id = session.get("user_id")
    can_manage_memberships = bool(
        actor_user_id
        and business_id
        and memberships.can_manage_memberships(actor_user_id, business_id)
    )
    return render_template(
        "admin.html",
        appointments=get_appointments(status=status, appointment_date=appointment_date, business_id=business_id),
        counts=get_appointment_counts(business_id),
        selected_status=status,
        selected_date=appointment_date or "",
        business_name=settings["business_name"],
        business_initials=settings["business_initials"],
        business_settings=settings,
        config_message=request.args.get("config_message", ""),
        config_error=request.args.get("config_error", ""),
        services=get_all_services_scoped(business_id),
        service_message=request.args.get("service_message", ""),
        service_error=request.args.get("service_error", ""),
        can_manage_memberships=can_manage_memberships,
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
    )

    return redirect(_admin_url(
        config_message="La configuración del negocio se guardó correctamente.",
    ))


@app.route("/admin/turnos/<int:appointment_id>/cancelar", methods=["POST"])
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
    return redirect(_admin_url(status=status if status in {"confirmed", "cancelled"} else "confirmed"))


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


        response = ask_ai(
            message,
            conversation,
            business_id=get_current_business_id(),
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
# API - DISPONIBILIDAD
# ============================================================

def _get_public_availability_response(fecha, business_id):
    if not is_api_request_allowed(get_client_ip(), "api:disponibilidad", business_id):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429

    try:
        horarios = get_available_times(fecha, business_id, request.args.get("servicio"))
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
        # CREAR TURNO
        # ----------------------------------------------------

        resultado = create_appointment(

            customer_name=nombre,

            phone=telefono,

            service=servicio,

            appointment_date=fecha,

            appointment_time=hora,

            business_id=business_id,
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

            return jsonify({

                "success": True,

                "appointment_id": resultado.get(
                    "appointment_id"
                ),
                "management_token": resultado.get("management_token"),

                "message": "El turno fue reservado correctamente."
            }), 201

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

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400


        resultado = cancel_appointment(
            appointment_id,
            telefono,
            business_id,
            management_token,
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


        if not nueva_fecha or not nueva_hora:

            return jsonify({
                "success": False,
                "error": "La nueva fecha y hora son obligatorias."
            }), 400

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400


        resultado = reschedule_appointment(

            appointment_id,

            nueva_fecha,

            nueva_hora,

            telefono,

            business_id,
            management_token,
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
