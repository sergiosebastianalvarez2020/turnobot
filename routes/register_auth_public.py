"""
Registro de rutas de autenticación pública - forgot, reset y registro.
"""

from routes.auth_public import forgot_password, registro, reset_password


def register(app):
    """Registra las rutas de autenticación pública.

    Args:
        app: Instancia de Flask application.
    """
    # Auth Public routes - forgot, reset, registro
    app.add_url_rule(
        "/forgot", endpoint="forgot_password", view_func=forgot_password, methods=["GET", "POST"]
    )
    app.add_url_rule("/registro", endpoint="registro", view_func=registro, methods=["GET", "POST"])
    app.add_url_rule(
        "/reset/<token>",
        endpoint="reset_password",
        view_func=reset_password,
        methods=["GET", "POST"],
    )
