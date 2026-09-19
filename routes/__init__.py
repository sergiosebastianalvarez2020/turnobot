"""
Registro de Blueprints y rutas de la aplicación.
"""

from routes.health import health_bp
from routes.public import index, business_index
from routes.auth import login, login_slug, logout, logout_slug


def register_blueprints(app):
    """Registra todos los blueprints y rutas en la aplicación Flask.

    Args:
        app: Instancia de Flask application.
    """
    from routes.health import health

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

    # Auth routes - login, logout, session helpers
    # Registrados via add_url_rule() para preservar endpoint names globales
    # (login, login_slug, logout, logout_slug).
    # NOTA: Flask 3.x no permite Blueprint(''), por lo que auth_bp no se
    # registra como Blueprint. En su lugar, las views se registran directamente.
    app.add_url_rule(
        "/login",
        endpoint="login",
        view_func=login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/b/<slug>/login",
        endpoint="login_slug",
        view_func=login_slug,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/logout",
        endpoint="logout",
        view_func=logout,
        methods=["POST"],
    )
    app.add_url_rule(
        "/b/<slug>/logout",
        endpoint="logout_slug",
        view_func=logout_slug,
        methods=["POST"],
    )
