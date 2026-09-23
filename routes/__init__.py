"""
Orquestador del registro de rutas de la aplicación.

Cada dominio de rutas se registra en su módulo dedicado ``register_*.py``.
Este módulo solo coordina el orden de registro, que replica el orden
histórico del archivo monolítico original.
"""

from routes import (
    register_admin,
    register_auth,
    register_auth_public,
    register_health,
    register_invitation,
    register_public,
    register_public_api,
    register_superadmin,
)

__all__ = ["register_blueprints"]


def register_blueprints(app):
    """Registra todos los blueprints y rutas en la aplicación Flask.

    Se delega por dominio en los módulos ``register_*.py``. Los endpoint names
    globales (``login``, ``login_slug``, ``index``, ``health``, etc.) se
    preservan vía ``app.add_url_rule()``; no se utilizan Blueprints.

    Args:
        app: Instancia de Flask application.
    """
    register_health.register(app)
    register_public.register(app)
    register_public_api.register(app)
    register_auth.register(app)
    register_auth_public.register(app)
    register_superadmin.register(app)
    register_invitation.register(app)
    register_admin.register(app)
