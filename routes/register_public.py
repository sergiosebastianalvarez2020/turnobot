"""
Registro de rutas públicas - landing page, wizard de reserva y contexto de negocio.
"""

from routes.public import (
    index,
    business_index,
    business_reservar_wizard,
    business_reservar_wizard_fecha,
    business_reservar_wizard_datos,
    business_reservar_wizard_confirmar,
)


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
    # Wizard de reserva visual
    app.add_url_rule(
        "/b/<slug>/reservar",
        endpoint="business_reservar_wizard",
        view_func=business_reservar_wizard,
        methods=["GET"],
    )
    app.add_url_rule(
        "/b/<slug>/reservar/fecha",
        endpoint="business_reservar_wizard_fecha",
        view_func=business_reservar_wizard_fecha,
        methods=["GET"],
    )
    app.add_url_rule(
        "/b/<slug>/reservar/datos",
        endpoint="business_reservar_wizard_datos",
        view_func=business_reservar_wizard_datos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/b/<slug>/reservar/confirmar",
        endpoint="business_reservar_wizard_confirmar",
        view_func=business_reservar_wizard_confirmar,
        methods=["GET", "POST"],
    )