"""Configuración consolidada del core de la aplicación (Bloque 5B).

build_config(app) aplica toda la configuración que vivía en app.py y devuelve
un dict con los valores clave para su re-export por parte de la fachada.
"""

import datetime
import os
import secrets

from werkzeug.middleware.proxy_fix import ProxyFix


def build_config(app, logger=None):
    """Configura la instancia Flask: secret, política de cookies, límites,
    ProxyFix y lifetime de sesión. Devuelve los valores re-exportados por app."""
    if os.getenv("FLASK_ENV") == "production" and not os.getenv("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY es obligatoria en producción")

    app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
    admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if os.getenv("FLASK_ENV") == "production" and admin_password:
        raise RuntimeError("ADMIN_PASSWORD fue eliminado; use ADMIN_PASSWORD_HASH")
    if not admin_password_hash and logger is not None:
        logger.warning(
            "ADMIN_PASSWORD_HASH no está configurada: acceso administrativo deshabilitado"
        )
    if os.getenv("FLASK_ENV") == "production" and (
        not admin_password_hash or os.getenv("COOKIE_SECURE") != "1"
    ):
        raise RuntimeError("ADMIN_PASSWORD_HASH es obligatoria en producción")
    if os.getenv("FLASK_ENV") == "production" and not os.getenv("SMTP_HOST") and logger is not None:
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
        trusted_proxy_count = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
    except ValueError as error:
        raise RuntimeError("TRUSTED_PROXY_COUNT debe ser un entero >= 0") from error
    if trusted_proxy_count < 0:
        raise RuntimeError("TRUSTED_PROXY_COUNT debe ser un entero >= 0")
    if trusted_proxy_count:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxy_count,
            x_proto=trusted_proxy_count,
            x_host=trusted_proxy_count,
            x_port=trusted_proxy_count,
            x_prefix=trusted_proxy_count,
        )

    default_lifetime = int(os.getenv("SESSION_LIFETIME_SECONDS", "86400"))
    app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(seconds=default_lifetime)

    return {
        "SECRET_KEY": app.secret_key,
        "ADMIN_PASSWORD_HASH": admin_password_hash,
        "ADMIN_PASSWORD": admin_password,
        "TRUSTED_PROXY_COUNT": trusted_proxy_count,
    }
