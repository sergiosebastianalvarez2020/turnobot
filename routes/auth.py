"""
Módulo de rutas de autenticación: login, logout y helpers de sesión.

Este módulo NO define un Blueprint registrado. En su lugar, expone las
view functions y helpers que se registran vía register_blueprints() en
routes/__init__.py usando add_url_rule(), preservando los endpoint names
globales (login, login_slug, logout, logout_slug).

Esto es consistente con el patrón ya establecido:
- routes/public.py: módulo de rutas (NOT a registered Blueprint)
- routes/health.py: Blueprint definido pero NO registrado; view registrada via add_url_rule()

NOTA SOBRE name='':
AGENTS.md indica que name='' está reservado para auth_bp. Sin embargo,
Flask 3.x rechaza Blueprint('') con ValueError. Por lo tanto, auth_bp
no se registra como Blueprint — en su lugar, las rutas se registran vía
add_url_rule() preservando los endpoint names, manteniendo la misma
arquitectura de routes/public.py y routes/health.py.

La migración es estructural: la lógica de negocio permanece idéntica;
únicamente cambia el ownership del código (app.py -> routes/auth.py).
"""

import datetime
import secrets
from functools import wraps

from flask import abort, current_app, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from application.rate_limit import is_login_request_allowed
from application.requests import get_client_ip
from application.session_crypto import _hash_session_token, _now_iso
from database.database import (
    create_session_scoped,
    get_user_by_email_scoped,
    get_user_by_id_scoped,
    is_session_valid_scoped,
    revoke_all_sessions_scoped,
)
from extensions import csrf_token, valid_csrf_token


def get_current_business_id():
    """Devuelve el negocio asociado al request actual, si existe."""
    business = getattr(g, "current_business", None)
    return business["id"] if business else None


def _login_url():
    business_id = get_current_business_id()
    if business_id == 1:
        return url_for("login")
    return url_for("login_slug", slug=g.current_business["slug"])


def _clear_business_session():
    """Invalidate ONLY the business (tenant) session."""
    session.pop("user_id", None)
    session.pop("session_token", None)


def _establish_session(user_id):
    """Crea una sesión persistente tras un login exitoso.

    Previene session fixation: limpia completamente la sesión anterior
    y establece una nueva. Preserva únicamente el contexto legítimo
    de plataforma (superadmin).
    """
    # Preservar solo datos legítimos de plataforma (superadmin)
    platform_user_id = session.get("platform_user_id")
    platform_token = session.get("platform_session_token")

    # Limpiar sesión completa e iniciar una nueva
    session.clear()

    # Restaurar solo datos legítimos de plataforma (superadmin)
    if platform_user_id is not None:
        session["platform_user_id"] = platform_user_id
    if platform_token is not None:
        session["platform_session_token"] = platform_token

    # Establecer nueva sesión de negocio con nuevo token
    session["user_id"] = user_id
    token = secrets.token_urlsafe(48)
    session["session_token"] = token
    session.permanent = True
    session["csrf_token"] = csrf_token()

    lifetime = current_app.config["PERMANENT_SESSION_LIFETIME"]
    expires_at = (datetime.datetime.now(datetime.UTC) + lifetime).strftime("%Y-%m-%d %H:%M:%S")
    create_session_scoped(user_id, _hash_session_token(token), expires_at)


def _authenticate_login(business, email, password):
    """
    Autentica email+password contra users/business_users para el negocio dado.
    Retorna (user_id, None) o (None, mensaje_error).

    NOTA: La migración del hash de bootstrap (ADMIN_PASSWORD_HASH) a un owner
    real ya NO ocurre aquí. La migración debe ejecutarse explícitamente vía
    script CLI o durante el provisioning inicial. Esto evita que la credencial
    de bootstrap permita takeover del owner tras el provisioning inicial.
    """
    from services.memberships import ROLE_CUSTOMER, get_membership_scoped

    if not is_login_request_allowed(_get_client_ip(), business["id"] if business else None):
        return None, "Demasiados intentos. Esperá unos minutos."

    if business is None:
        return None, "Credenciales inválidas."

    email = (email or "").strip().lower()
    user = get_user_by_email_scoped(email) if email else None

    if user is not None:
        if not user["active"]:
            return None, "Credenciales inválidas."
        membership = get_membership_scoped(user["id"], business["id"])
        if not membership or membership["role_name"] == ROLE_CUSTOMER:
            return None, "Credenciales inválidas."
        if not password or not check_password_hash(user["password_hash"], password):
            return None, "Credenciales inválidas."
        return user["id"], None

    return None, "Credenciales inválidas."


def _get_client_ip():
    return get_client_ip()


def _is_authenticated():
    """Valida una sesión activa (usuario y sesión persistente válida para el negocio actual)."""
    user_id = session.get("user_id")
    token = session.get("session_token")
    if not user_id or not token:
        return False

    user = get_user_by_id_scoped(user_id)
    if not user or not user["active"]:
        return False

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


def login():
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template(
                "login.html", error=True, error_message="La sesión expiró. Intentá nuevamente."
            ), 400
        user_id, error = _authenticate_login(
            g.current_business, request.form.get("email", ""), request.form.get("password", "")
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


def login_slug(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)

    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template(
                "login.html", error=True, error_message="La sesión expiró. Intentá nuevamente."
            ), 400
        user_id, error = _authenticate_login(
            business, request.form.get("email", ""), request.form.get("password", "")
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


def logout():
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user_id = session.get("user_id")
    if user_id:
        revoke_all_sessions_scoped(user_id)
    _clear_business_session()
    return redirect(url_for("login"))


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
