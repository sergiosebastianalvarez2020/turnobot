"""
Registro de rutas de superadmin - nivel plataforma.
"""

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


def register(app):
    """Registra las rutas de superadmin.

    Args:
        app: Instancia de Flask application.
    """
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