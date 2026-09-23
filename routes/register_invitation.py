"""
Registro de rutas de invitaciones - staff, públicas y panel admin.
"""

from routes.invitation import admin_usuarios_invitar_enlace, public_invitation, staff_invitation


def register(app):
    """Registra las rutas de invitaciones.

    Args:
        app: Instancia de Flask application.
    """
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
