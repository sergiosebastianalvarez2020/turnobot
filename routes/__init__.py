"""
Registro de Blueprints y rutas de la aplicación.
"""

def register_blueprints(app):
    """Registra todos los blueprints y rutas en la aplicación Flask.

    Args:
        app: Instancia de Flask application.
    """
    from routes.health import health
    from routes.public import index, business_index

    # Health - sin url_prefix, sin autenticación, sin tenant context
    app.add_url_rule(
        "/health",
        endpoint="health",
        view_func=health,
        methods=["GET"],
    )

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