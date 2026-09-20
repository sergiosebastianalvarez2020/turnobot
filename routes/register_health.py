"""
Registro de rutas de healthcheck.
"""

from routes.health import health


def register(app):
    """Registra las rutas de healthcheck.

    Args:
        app: Instancia de Flask application.
    """
    # Health - sin url_prefix, sin autenticación, sin tenant context
    app.add_url_rule(
        "/health",
        endpoint="health",
        view_func=health,
        methods=["GET"],
    )