"""Paquete `application` — core de la aplicación (Bloque 5B).

create_app() es la factory que construye la instancia Flask: configuración,
contexto tenant, seguridad HTTP, error handlers y registro de blueprints.

create_app() es standalone-safe (Bloque 7A): puede importarse y ejecutarse en un
proceso limpio, sin que el módulo de fachada `app` haya sido importado antes.
Por eso este módulo NO importa routes en el top-level (evita ciclos y mantiene
el import de la factory barato): las rutas se importan dentro de create_app().
"""

import os

from flask import Flask

from application.config import build_config
from application.logging_config import (
    USE_JSON_LOGS,
    RequestContextFilter,
    RequestIdFilter,
    StructuredFormatter,
    configure_logging,
    logger,
)

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PACKAGE_DIR)


def create_app():
    """Crea y configura la instancia Flask de la aplicación."""
    app = Flask(
        __name__,
        root_path=_PROJECT_ROOT,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )

    config = build_config(app, logger=logger)
    app.ADMIN_PASSWORD_HASH = config["ADMIN_PASSWORD_HASH"]
    app.ADMIN_PASSWORD = config["ADMIN_PASSWORD"]
    app.TRUSTED_PROXY_COUNT = config["TRUSTED_PROXY_COUNT"]

    from database.database import init_database

    init_database()

    # Contexto tenant y de negocio
    from application.tenant import (
        inject_admin_prefix,
        inject_business_settings,
        load_current_business,
    )

    app.before_request(load_current_business)
    app.context_processor(inject_admin_prefix)
    app.context_processor(inject_business_settings)

    # Seguridad HTTP global (orden preservado: after_request, handlers, before_request)
    from application.security import (
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

    app.after_request(add_security_headers)
    app.errorhandler(400)(handle_400)
    app.errorhandler(403)(handle_403)
    app.errorhandler(404)(handle_404)
    app.errorhandler(405)(handle_405)
    app.errorhandler(429)(handle_429)
    app.errorhandler(413)(handle_413)
    app.errorhandler(500)(handle_500)
    app.errorhandler(Exception)(handle_unhandled_exception)
    app.before_request(_reject_oversized_requests)

    from extensions import csrf_token

    app.jinja_env.globals["csrf_token"] = csrf_token

    # Registro de blueprints/rutas (import lazy para evitar ciclos)
    from routes import register_blueprints

    register_blueprints(app)

    return app
