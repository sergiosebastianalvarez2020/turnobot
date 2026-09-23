"""
Registro de rutas de autenticación - login, logout y helpers de sesión.
"""

from routes.auth import login, login_slug, logout, logout_slug


def register(app):
    """Registra las rutas de autenticación.

    Args:
        app: Instancia de Flask application.
    """
    # Auth routes - login, logout, session helpers
    # Registrados via add_url_rule() para preservar endpoint names globales
    # (login, login_slug, logout, logout_slug).
    # NOTA: Flask 3.x no permite Blueprint(''), por lo que auth_bp no se
    # registra como Blueprint. En su lugar, las views se registran directamente.
    app.add_url_rule("/login", endpoint="login", view_func=login, methods=["GET", "POST"])
    app.add_url_rule(
        "/b/<slug>/login", endpoint="login_slug", view_func=login_slug, methods=["GET", "POST"]
    )
    app.add_url_rule("/logout", endpoint="logout", view_func=logout, methods=["POST"])
    app.add_url_rule(
        "/b/<slug>/logout", endpoint="logout_slug", view_func=logout_slug, methods=["POST"]
    )
