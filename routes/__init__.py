"""
Registro de Blueprints y rutas de la aplicación.
"""

from routes.health import health_bp
from routes.public import index, business_index
from routes.auth import login, login_slug, logout, logout_slug
from routes.auth_public import forgot_password, registro, reset_password
from routes.superadmin import (
    superadmin_login,
    superadmin_logout,
    superadmin_panel,
    superadmin_audit,
    superadmin_negocios_crear,
    superadmin_negocios_detalle,
    superadmin_negocios_aprobar,
    superadmin_negocios_desactivar,
    superadmin_negocios_activar,
    superadmin_negocios_reinviar,
)
from routes.invitation import (
    staff_invitation,
    public_invitation,
    admin_usuarios_invitar_enlace,
)


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

    # Auth Public routes - forgot, reset, registro
    app.add_url_rule(
        "/forgot",
        endpoint="forgot_password",
        view_func=forgot_password,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/registro",
        endpoint="registro",
        view_func=registro,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/reset/<token>",
        endpoint="reset_password",
        view_func=reset_password,
        methods=["GET", "POST"],
    )

    # Superadmin routes - platform level
    app.add_url_rule(
        "/superadmin/login",
        endpoint="superadmin_login",
        view_func=superadmin_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/superadmin/logout",
        endpoint="superadmin_logout",
        view_func=superadmin_logout,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin",
        endpoint="superadmin_panel",
        view_func=superadmin_panel,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin/auditoria",
        endpoint="superadmin_audit",
        view_func=superadmin_audit,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin/negocios/crear",
        endpoint="superadmin_negocios_crear",
        view_func=superadmin_negocios_crear,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/negocios/<int:business_id>",
        endpoint="superadmin_negocios_detalle",
        view_func=superadmin_negocios_detalle,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin/negocios/<int:business_id>/aprobar",
        endpoint="superadmin_negocios_aprobar",
        view_func=superadmin_negocios_aprobar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/negocios/<int:business_id>/desactivar",
        endpoint="superadmin_negocios_desactivar",
        view_func=superadmin_negocios_desactivar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/negocios/<int:business_id>/activar",
        endpoint="superadmin_negocios_activar",
        view_func=superadmin_negocios_activar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/negocios/<int:business_id>/reinviar",
        endpoint="superadmin_negocios_reinviar",
        view_func=superadmin_negocios_reinviar,
        methods=["POST"],
    )

    # Invitation routes - staff, public, admin panel
    app.add_url_rule(
        "/b/<slug>/invitacion-staff/<token>",
        endpoint="staff_invitation",
        view_func=staff_invitation,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/b/<slug>/invitacion/<token>",
        endpoint="public_invitation",
        view_func=public_invitation,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/admin/usuarios/invitar-enlace",
        endpoint="admin_usuarios_invitar_enlace",
        view_func=admin_usuarios_invitar_enlace,
        methods=["POST"],
    )
    app.add_url_rule(
        "/b/<slug>/admin/usuarios/invitar-enlace",
        endpoint="admin_usuarios_invitar_enlace",
        view_func=admin_usuarios_invitar_enlace,
        methods=["POST"],
    )