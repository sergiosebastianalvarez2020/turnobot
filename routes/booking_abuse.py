"""Comprobaciones anti-abuso compartidas para los flujos públicos de reserva.

Centraliza la lógica de honeypot, tiempo mínimo y cupo por contacto
para que los dos puntos de entrada (wizard y API) no la dupliquen.
"""

from flask import jsonify

from application.requests import check_form_timing, contact_limit_response, is_honeypot_triggered
from services.appointments import get_contact_booking_limit


def enforce_public_booking_abuse_checks(data, phone, email, business_id):
    """Ejecuta las comprobaciones anti-abuso del flujo público de reserva.

    Args:
        data: dict con el cuerpo de la request (honeypot, form_rendered_at).
        phone: teléfono ya normalizado (solo dígitos).
        email: email ya normalizado (minúsculas).
        business_id: ID del negocio.

    Returns:
        None si pasa todas las comprobaciones, o una tupla (Response, status_code)
        con la respuesta de error correspondiente.
    """
    # 1) Honeypot: campo oculto de nombre aleatorio completado por un bot.
    if is_honeypot_triggered(data):
        return (
            jsonify(
                {
                    "success": False,
                    "code": "HONEYPOT_TRIGGERED",
                    "error": "No se pudo procesar la solicitud.",
                }
            ),
            400,
        )

    # 2) Tiempo mínimo de llenado (timestamp renderizado por el servidor).
    form_ok, _form_reason = check_form_timing(data)
    if not form_ok:
        return (
            jsonify(
                {
                    "success": False,
                    "code": "FORM_TOO_FAST",
                    "error": "Revisá los datos con calma y volvé a enviar el formulario.",
                }
            ),
            400,
        )

    # 3) Cupo de turnos activos por contacto (teléfono/email).
    contact_limit = get_contact_booking_limit(phone, email, business_id)
    if contact_limit is not None:
        return contact_limit_response(contact_limit), 429

    return None
