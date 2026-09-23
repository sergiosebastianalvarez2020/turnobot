"""Wrappers de notificaciones mockeables (Bloque 5B, Paso 5).

send_approved_invitation_email y send_business_confirmation_email extraídos
de app.py, delegando en services.notifications con lógica idéntica.
"""

from services import notifications as notifications_service


def send_approved_invitation_email(
    owner_email, business_name, invitation_link, expires_at="", lifetime_hours=None
):
    """Wrapper para EMAIL 2: invitación aprobada al owner."""
    return notifications_service.send_approved_invitation_email(
        owner_email, business_name, invitation_link, expires_at, lifetime_hours
    )


def send_business_confirmation_email(business_id, appointment, force=False):
    """Wrapper para EMAIL 5: aviso de reserva al negocio."""
    return notifications_service.send_business_confirmation_email(business_id, appointment, force)
