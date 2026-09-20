"""
Registro de rutas públicas - landing page y contexto de negocio.
"""

from routes.public import index, business_index


def register(app):
    """Registra las rutas públicas.

    Args:
        app: Instancia de Flask application.
    """
    # Public routes - landing page y contexto de negocio
    app.add_url_rule(
        "/",
        endpoint="index",
        view_func=index,
        methods=["GET"],
    )
    app.add_url_rule(
        "/b/<slug>",
        endpoint="business_index",
        view_func=business_index,
        methods=["GET"],
    )