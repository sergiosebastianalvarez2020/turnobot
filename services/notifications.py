"""Sistema de notificaciones desacoplado por canal.

Hoy implementa el canal `email` (SMTP vía stdlib `smtplib`). El diseño
expone un único punto de entrada `send_appointment_notification` con el
parámetro `channel`, de modo que agregar WhatsApp más adelante implica
implementar un nuevo canal sin rehacer el resto del sistema.

Reglas:
- El envío está scoped por `business_id` y solo se hace si el negocio tiene
  las notificaciones habilitadas (`notifications_enabled`).
- Es idempotente: se registra cada envío en `notification_log` y se saltea
  si ya se envió ese tipo/canal para el turno.
- Nunca se reintenta dentro de la reserva: si SMTP falla, se loguea y la
  reserva sigue su curso (no se bloquea al cliente).
- El plaintext del `management_token` solo existe al momento de crear el
  turno (hash en DB). Por eso la CONFIRMACIÓN incluye enlaces seguros, pero
  el RECORDATORIO (que se ejecuta después, sin el plaintext) no: solo recuerda
  el turno y remite a gestionar con el mail de confirmación o el asistente.
"""

import hashlib
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from database.database import (
    add_notification_log_scoped,
    get_business_settings_scoped,
    notification_sent_scoped,
)

logger = logging.getLogger("turnobot.notifications")

CONFIRMATION = "confirmation"
REMINDER = "reminder"


# ============================================================
# Config SMTP (global, por entorno)
# ============================================================

def smtp_configured():
    return bool(os.getenv("SMTP_HOST"))


def _smtp_config():
    return {
        "host": os.getenv("SMTP_HOST", ""),
        "port": int(os.getenv("SMTP_PORT", "587") or 587),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_addr": os.getenv("EMAIL_FROM", os.getenv("SMTP_USER", "")),
        "from_name": os.getenv("EMAIL_FROM_NAME", "TurnoBot"),
    }


def notifications_enabled(business_id):
    """True si el negocio tiene habilitadas las notificaciones."""
    settings = get_business_settings_scoped(business_id)
    if not settings:
        return False
    return bool(settings["notifications_enabled"])


# ============================================================
# Envío SMTP (canal email)
# ============================================================

def _build_message(to_addr, subject, html, text, cfg):
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    message["To"] = to_addr
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))
    return message


def _send_email(to_addr, subject, html, text):
    """Envía un email por SMTP. Devuelve True/False y nunca lanza."""
    if not smtp_configured():
        logger.warning("SMTP no configurado; no se envía email a %s", to_addr)
        return False
    cfg = _smtp_config()
    try:
        message = _build_message(to_addr, subject, html, text, cfg)
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.ehlo()
            if cfg["port"] == 587 or cfg.get("user"):
                # STARTTLS siempre que haya credenciales o puerto estándar.
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException:
                    pass
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["from_addr"], [to_addr], message.as_string())
        return True
    except Exception:
        logger.exception("Error enviando email a %s", to_addr)
        return False


# ============================================================
# Plantillas
# ============================================================

def _esc(value):
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_hour(value):
    return str(value) if value else ""


def _build_confirmation(appointment, business, management_url):
    business_name = _esc(business.get("business_name") or "Mi negocio")
    name = _esc(appointment.get("customer_name") or "cliente")
    service = _esc(appointment.get("service") or "")
    date = _esc(appointment.get("appointment_date") or "")
    time = _fmt_hour(appointment.get("appointment_time"))
    end = _fmt_hour(appointment.get("appointment_end"))

    text = (
        f"Hola {name},\n\n"
        f"Tu turno en {business_name} fue confirmado.\n\n"
        f"Servicio: {service}\n"
        f"Fecha: {date}\n"
        f"Horario: {time}"
    )
    if end and end != time:
        text += f" a {end}"
    text += "\n"
    if management_url:
        text += (
            "\nPodés ver o cancelar tu turno acá:\n"
            f"{management_url}\n"
        )
    text += "\n¡Te esperamos!\n" + business_name

    rows = (
        f"<tr><td><strong>Servicio</strong></td><td>{service}</td></tr>"
        f"<tr><td><strong>Fecha</strong></td><td>{date}</td></tr>"
        f"<tr><td><strong>Horario</strong></td><td>{time}"
        + (f" a {end}" if end and end != time else "")
        + "</td></tr>"
    )
    link_html = (
        f'<p><a href="{management_url}" '
        f'style="background:#1463FF;color:#fff;padding:10px 18px;'
        f'border-radius:8px;text-decoration:none;">Ver y gestionar mi turno</a></p>'
        if management_url else ""
    )
    html = (
        f"<h2>Tu turno fue confirmado</h2>"
        f"<p>Hola {name}, confirmamos tu turno en <strong>{business_name}</strong>.</p>"
        f"<table>{rows}</table>{link_html}"
        f"<p>¡Te esperamos!</p>"
    )
    return text, html


def _build_reminder(appointment, business):
    business_name = _esc(business.get("business_name") or "Mi negocio")
    name = _esc(appointment.get("customer_name") or "cliente")
    service = _esc(appointment.get("service") or "")
    date = _esc(appointment.get("appointment_date") or "")
    time = _fmt_hour(appointment.get("appointment_time"))
    end = _fmt_hour(appointment.get("appointment_end"))

    text = (
        f"Hola {name},\n\n"
        f"Te recordamos tu turno en {business_name} mañana.\n\n"
        f"Servicio: {service}\n"
        f"Fecha: {date}\n"
        f"Horario: {time}"
    )
    if end and end != time:
        text += f" a {end}"
    text += (
        "\n\n¿No podés asistir? Revisá tu correo de confirmación "
        "o consultá con el asistente del negocio.\n\n¡Nos vemos!\n" + business_name
    )

    rows = (
        f"<tr><td><strong>Servicio</strong></td><td>{service}</td></tr>"
        f"<tr><td><strong>Fecha</strong></td><td>{date}</td></tr>"
        f"<tr><td><strong>Horario</strong></td><td>{time}"
        + (f" a {end}" if end and end != time else "")
        + "</td></tr>"
    )
    html = (
        f"<h2>Recordatorio de turno</h2>"
        f"<p>Hola {name}, te recordamos tu turno en <strong>{business_name}</strong>.</p>"
        f"<table>{rows}</table>"
        f"<p>¿No podés asistir? Consultá el correo de confirmación o "
        f"al asistente del negocio para reprogramar o cancelar.</p>"
    )
    return text, html


# ============================================================
# Despacho
# ============================================================

def send_appointment_notification(
    business_id,
    appointment,
    notif_type,
    channel="email",
    management_token=None,
    slug=None,
    public_base_url="",
    force=False,
):
    """Envía una notificación de turno.

    Devuelve (sent: bool, reason: str|None).
    `sent=True` solo si realmente se despachó y se registró.
    """
    destination = (appointment.get("customer_email") or "").strip()
    if not destination:
        return False, "no_destination"

    if not smtp_configured():
        return False, "smtp_not_configured"

    if not force and not notifications_enabled(business_id):
        return False, "disabled"

    if notification_sent_scoped(business_id, appointment["id"], notif_type, channel):
        return False, "already_sent"

    business = get_business_settings_scoped(business_id)
    business = dict(business) if business is not None else {}

    if notif_type == CONFIRMATION:
        subject = f"Confirmación de tu turno - {business.get('business_name') or 'Mi negocio'}"
        management_url = ""
        if management_token and slug:
            base = (public_base_url or "").rstrip("/")
            management_url = f"{base}/b/{slug}/turno/{management_token}?id={appointment.get('id', '')}"
        text, html = _build_confirmation(appointment, business, management_url)
    elif notif_type == REMINDER:
        subject = f"Recordatorio: tu turno mañana - {business.get('business_name') or 'Mi negocio'}"
        text, html = _build_reminder(appointment, business)
    else:
        return False, "unknown_type"

    ok = _send_email(destination, subject, html, text)
    if not ok:
        return False, "send_failed"
    add_notification_log_scoped(
        appointment["id"], business_id, notif_type, channel, destination
    )
    return True, None


def send_confirmation_email(
    business_id,
    appointment,
    management_token=None,
    slug=None,
    public_base_url="",
    force=False,
):
    return send_appointment_notification(
        business_id,
        appointment,
        CONFIRMATION,
        channel="email",
        management_token=management_token,
        slug=slug,
        public_base_url=public_base_url,
        force=force,
    )


def send_reminder_email(business_id, appointment, force=False):
    return send_appointment_notification(
        business_id,
        appointment,
        REMINDER,
        channel="email",
        force=force,
    )
