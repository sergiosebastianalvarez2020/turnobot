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

import datetime
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from database.database import (
    claim_notification_scoped,
    get_business_settings_scoped,
    get_notification_state_scoped,
    list_platform_users,
    notification_sent_scoped,  # noqa: F401  (re-export público, usado por tests/callers)
    upsert_notification_log_scoped,
)

logger = logging.getLogger("turnobot.notifications")

CONFIRMATION = "confirmation"
REMINDER = "reminder"
BUSINESS_CONFIRMATION = "business_confirmation"

# Claves de entorno cuyos valores NUNCA deben filtrarse en logs/mensajes de error.
_SENSITIVE_ENV_KEYS = ("SMTP_PASSWORD",)


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


def _now_iso():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")


def notifications_enabled(business_id):
    """True si el negocio tiene habilitadas las notificaciones."""
    settings = get_business_settings_scoped(business_id)
    if not settings:
        return False
    return bool(settings["notifications_enabled"])


# ============================================================
# Higiene de valores para email y logs
# ============================================================


def _clean_header(value):
    """Elimina CR/LF de encabezados para impedir inyección de cabeceras SMTP."""
    return str(value or "").replace("\r", " ").replace("\n", " ")


def _safe_error(value):
    """Error técnico SANITIZADO: sin secretos de entorno, una sola línea y corto.

    Previene fuga de SMTP_PASSWORD y CRLF en `notification_log.error`.
    """
    value = str(value or "")
    for key in _SENSITIVE_ENV_KEYS:
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value.replace("\r", " ").replace("\n", " ")[:200]


def _esc(value):
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_hour(value):
    return str(value) if value else ""


def _build_message(to_addr, subject, html, text, cfg):
    message = MIMEMultipart("alternative")
    message["Subject"] = _clean_header(subject)
    message["From"] = formataddr((_clean_header(cfg["from_name"]), cfg["from_addr"]))
    message["To"] = _clean_header(to_addr)
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))
    return message


def _send_email(to_addr, subject, html, text):
    """Envía un email por SMTP.

    Devuelve (ok: bool, error: str): no lanza y el error ya viene sanitizado
    (`_safe_error`) para persistirlo en notification_log sin secretos ni CRLF.
    """
    if not smtp_configured():
        logger.warning("SMTP no configurado; no se envía email a %s", to_addr)
        return False, "SMTP no configurado"
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
        return True, ""
    except Exception as error:  # noqa: BLE001 - el envío NUNCA debe escalar
        detail = _safe_error(str(error).strip() or error.__class__.__name__)
        logger.error("Error enviando email a %s: %s", to_addr, detail)
        return False, detail


# ============================================================
# Plantillas
# ============================================================


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
        text += f"\n¿Necesitás cancelar o reprogramar? Gestioná tu turno acá:\n{management_url}\n"
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
        f'border-radius:8px;text-decoration:none;">Cancelar o reprogramar mi turno</a></p>'
        if management_url
        else ""
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

    Máquina de estados en `notification_log`:
      - fila con status='sent'  -> `already_sent` (idempotencia, no se reenvía)
      - fila con status='failed' -> se re-intenta (UPDATE de la misma fila)
      - sin fila                -> INSERT (status='pending' durante el envío,
                                   'sent'/'failed' según resultado)
    El envío jamás bloquea al cliente: ante fallo SMTP se registra status='failed'
    con error sanitizado y la reserva sigue su curso.
    """
    destination = (appointment.get("customer_email") or "").strip()
    if not destination:
        return False, "no_destination"

    if not smtp_configured():
        return False, "smtp_not_configured"

    if not force and not notifications_enabled(business_id):
        return False, "disabled"

    business = get_business_settings_scoped(business_id)
    business = dict(business) if business is not None else {}

    if notif_type == CONFIRMATION:
        subject = f"Confirmación de tu turno - {business.get('business_name') or 'Mi negocio'}"
        management_url = ""
        if management_token and slug:
            base = (public_base_url or "").rstrip("/")
            management_url = (
                f"{base}/b/{slug}/turno/{management_token}?id={appointment.get('id', '')}"
            )
        text, html = _build_confirmation(appointment, business, management_url)
    elif notif_type == REMINDER:
        subject = f"Recordatorio: tu turno mañana - {business.get('business_name') or 'Mi negocio'}"
        text, html = _build_reminder(appointment, business)
    else:
        return False, "unknown_type"

    if not claim_notification_scoped(
        appointment["id"], business_id, notif_type, channel, destination
    ):
        state = get_notification_state_scoped(business_id, appointment["id"], notif_type, channel)
        return False, "already_sent" if state and state.get("status") == "sent" else "in_progress"

    ok, detail = _send_email(destination, subject, html, text)
    if not ok:
        upsert_notification_log_scoped(
            appointment["id"],
            business_id,
            notif_type,
            channel,
            destination,
            status="failed",
            error=detail,
            last_attempt_at=_now_iso(),
        )
        return False, "send_failed"

    upsert_notification_log_scoped(
        appointment["id"],
        business_id,
        notif_type,
        channel,
        destination,
        status="sent",
        error="",
        last_attempt_at=_now_iso(),
    )
    return True, None


def send_confirmation_email(
    business_id, appointment, management_token=None, slug=None, public_base_url="", force=False
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
        business_id, appointment, REMINDER, channel="email", force=force
    )


# ============================================================
# EMAIL 5 - Aviso de reserva al negocio (by-tenant)
# ============================================================


def _business_destination(business):
    """Destino del EMAIL 5: business_settings.notification_email (scoped).

    Nunca se toma del cliente/request: solo de la configuración del tenant.
    """
    return (business.get("notification_email") or "").strip()


def _build_business_confirmation(appointment, business):
    business_name = _esc(business.get("business_name") or "Mi negocio")
    name = _esc(appointment.get("customer_name") or "cliente")
    service = _esc(appointment.get("service") or "")
    date = _esc(appointment.get("appointment_date") or "")
    time = _fmt_hour(appointment.get("appointment_time"))
    end = _fmt_hour(appointment.get("appointment_end"))
    window = time + (f" a {end}" if end and end != time else "")

    text = (
        f"Nueva reserva en {business_name}.\n\n"
        f"Cliente: {name}\n"
        f"Servicio: {service}\n"
        f"Fecha: {date}\n"
        f"Horario: {window}\n\n"
        "El cliente recibió su confirmación con opción de cancelar "
        "o reprogramar."
    )
    html = (
        f"<h2>¡Nueva reserva!</h2>"
        f"<p>Se registró una nueva reserva en <strong>{business_name}</strong>.</p>"
        f"<table>"
        f"<tr><td><strong>Cliente</strong></td><td>{name}</td></tr>"
        f"<tr><td><strong>Servicio</strong></td><td>{service}</td></tr>"
        f"<tr><td><strong>Fecha</strong></td><td>{date}</td></tr>"
        f"<tr><td><strong>Horario</strong></td><td>{window}</td></tr>"
        f"</table>"
        f"<p>El cliente recibió su confirmación con opción de cancelar "
        f"o reprogramar.</p>"
    )
    return text, html


def send_business_confirmation_email(business_id, appointment, force=False):
    """EMAIL 5: aviso de nueva reserva al email del negocio.

    Requiere notifications_enabled Y notification_email configurados por el
    tenant (scoped; nunca de otro negocio). No toca la reserva: si el aviso
    falla, registra status='failed' y el flujo sigue.
    """
    destination = _business_destination(dict(get_business_settings_scoped(business_id) or {}))
    if not destination:
        return False, "no_business_destination"
    if not smtp_configured():
        return False, "smtp_not_configured"
    if not force and not notifications_enabled(business_id):
        return False, "disabled"

    business = dict(get_business_settings_scoped(business_id) or {})
    text, html = _build_business_confirmation(appointment, business)
    subject = f"Nueva reserva - {business.get('business_name') or 'Mi negocio'}"

    if not claim_notification_scoped(
        appointment["id"], business_id, BUSINESS_CONFIRMATION, "email", destination
    ):
        state = get_notification_state_scoped(
            business_id, appointment["id"], BUSINESS_CONFIRMATION, "email"
        )
        return False, "already_sent" if state and state.get("status") == "sent" else "in_progress"
    ok, detail = _send_email(destination, subject, html, text)
    if not ok:
        upsert_notification_log_scoped(
            appointment["id"],
            business_id,
            BUSINESS_CONFIRMATION,
            "email",
            destination,
            status="failed",
            error=detail,
            last_attempt_at=_now_iso(),
        )
        return False, "send_failed"

    upsert_notification_log_scoped(
        appointment["id"],
        business_id,
        BUSINESS_CONFIRMATION,
        "email",
        destination,
        status="sent",
        error="",
        last_attempt_at=_now_iso(),
    )
    return True, None


def dispatch_booking_emails(
    business_id, appointment, business=None, management_token=None, slug=None, public_base_url=""
):
    """Despacha los emails de una reserva nueva (EMAIL 4 + EMAIL 5).

    Devuelve {"confirmation": (sent, reason), "business": (sent, reason)}.
    Cada envío es independiente: un fallo en el aviso al negocio NO revierte
    la reserva ni bloquea la confirmación al cliente.
    """
    confirmation = send_confirmation_email(
        business_id,
        appointment,
        management_token=management_token,
        slug=slug,
        public_base_url=public_base_url,
    )
    business_aviso = send_business_confirmation_email(business_id, appointment)
    return {"confirmation": confirmation, "business": business_aviso}


# ============================================================
# EMAIL DE PLATAFORMA (alta / aprobación / invitación)
# ============================================================


def _list_superadmin_emails():
    """Emails de superadmins ACTIVOS (destinatarios controlados por la DB)."""
    emails = []
    for user in list_platform_users() or []:
        if user.get("active") and (user.get("email") or "").strip():
            emails.append(user["email"].strip())
    return emails


def send_apply_confirmation_email(owner_email, business_name):
    """EMAIL 1: confirma al solicitante que su alta quedó registrada.

    No incluye tokens: solo informa que el negocio será revisado y que el
    alta se completa al aprobarse. Devuelve (sent: bool, reason: str|None).
    """
    business_name = business_name or "tu negocio"
    text = (
        f"Hola,\n\n"
        f"Recibimos tu solicitud de alta para {business_name}.\n"
        "La estamos revisando; cuando sea aprobada te llegará un enlace "
        "para configurar tu acceso y activar el negocio.\n\n"
        "Saludos,\nTurnoBot"
    )
    html = (
        f"<h2>Alta recibida</h2>"
        f"<p>Hola,</p>"
        f"<p>Recibimos tu solicitud de alta para <strong>{business_name}</strong>.</p>"
        f"<p>La estamos revisando. Cuando sea aprobada te llegará un enlace "
        f"para configurar tu acceso.</p>"
        f"<p>Saludos,<br>TurnoBot</p>"
    )
    subject = f"Tu alta en TurnoBot fue recibida - {business_name[:80]}"
    ok, detail = _send_email(owner_email, subject, html, text)
    if not ok:
        logger.error("EMAIL 1 falló a %s: %s", owner_email, detail)
    return ok, None if ok else "send_failed"


def send_apply_notice_to_superadmin(business_name, business_id=None):
    """EMAIL 1: avisa a los SUPERADMINES activos que hay un alta pendiente.

    Destinatarios desde DB (nunca de la request). Si no hay SMTP configurado o
    no existe superadmin activo, no falla la operación.
    """
    emails = _list_superadmin_emails()
    if not emails:
        return False, "no_superadmin_destination"
    if not smtp_configured():
        return False, "smtp_not_configured"
    business_name = business_name or "Un negocio"
    text = (
        f"Hay una solicitud de alta pendiente: {business_name}.\n"
        "Revisala en el panel superadmin para aprobar o rechazar.\n\n"
        "TurnoBot"
    )
    html = (
        f"<h2>Alta pendiente de aprobación</h2>"
        f"<p>Se registró una solicitud de alta para "
        f"<strong>{business_name}</strong>.</p>"
        f"<p>Revisá el panel superadmin para aprobar la solicitud.</p>"
        f"<p>TurnoBot</p>"
    )
    subject = f"Nueva solicitud de alta - {business_name[:80]}"
    sent_any = False
    for email in emails:
        ok, _ = _send_email(email, subject, html, text)
        sent_any = sent_any or ok
    return sent_any, None if sent_any else "send_failed"


def send_approved_invitation_email(
    owner_email, business_name, invitation_link, expires_at="", lifetime_hours=None
):
    """EMAIL 2: envía al owner la invitación para definir su contraseña.

    Un solo uso y vence a las 72 h (INVITATION_LIFETIME_HOURS). El enlace
    (token) viaja en este email al destinatario correcto; la aplicación NUNCA
    loguea el token (el superadmin lo ve en la respuesta HTTP de la aprobación).
    """
    business_name = business_name or "tu negocio"
    lifetime = lifetime_hours or 72
    text = (
        f"¡Tu negocio {business_name} fue aprobado!\n\n"
        "Configurá tu contraseña y activá el negocio con este enlace:\n"
        f"{invitation_link}\n\n"
        f"El enlace es de un solo uso y vence en {lifetime} horas. "
        "Si vence, el superadmin puede generar uno nuevo.\n\n"
        "Saludos,\nTurnoBot"
    )
    html = (
        f"<h2>¡Tu negocio fue aprobado!</h2>"
        f"<p>Hola,</p>"
        f"<p>Tu negocio <strong>{business_name}</strong> fue aprobado.</p>"
        f'<p><a href="{invitation_link}" '
        f'style="background:#1463FF;color:#fff;padding:10px 18px;'
        f'border-radius:8px;text-decoration:none;">Configurar mi contraseña</a></p>'
        f"<p>El enlace es de un solo uso y vence en {lifetime} horas.</p>"
        f"<p>Saludos,<br>TurnoBot</p>"
    )
    subject = f"{business_name} fue aprobado - configurá tu acceso"
    ok, detail = _send_email(owner_email, subject, html, text)
    if not ok:
        logger.error("EMAIL 2 falló a %s: %s", owner_email, detail)
    return ok, None if ok else "send_failed"


def send_staff_invitation_email(staff_email, invitation_link, business_name, role_name):
    """EMAIL 3: invitación de staff/admin a un negocio existente.

    El owner invita a un staff/admin por email. El destinatario recibe un enlace
    para establecer su contraseña y activar su cuenta. El token viaja solo en
    este email; la aplicación NUNCA loguea el token.

    Devuelve (sent: bool, reason: str|None).
    """
    business_name = business_name or "Mi negocio"
    role_label = "administrador" if role_name == "admin" else "miembro del equipo"
    lifetime = platform_service_invitation_lifetime()
    text = (
        f"Hola,\n\n"
        f"El dueño de {business_name} te invitó a unirte como {role_label}.\n"
        "Configurá tu contraseña con este enlace:\n"
        f"{invitation_link}\n\n"
        f"El enlace es de un solo uso y vence en {lifetime} horas.\n\n"
        "Saludos,\nTurnoBot"
    )
    html = (
        f"<h2>Invitación a {business_name}</h2>"
        f"<p>Hola,</p>"
        f"<p>El dueño de <strong>{business_name}</strong> te invitó a unirte "
        f"como <strong>{role_label}</strong>.</p>"
        f'<p><a href="{invitation_link}" '
        f'style="background:#1463FF;color:#fff;padding:10px 18px;'
        f'border-radius:8px;text-decoration:none;">Configurar mi contraseña</a></p>'
        f"<p>El enlace es de un solo uso y vence en {lifetime} horas.</p>"
        f"<p>Saludos,<br>TurnoBot</p>"
    )
    subject = f"Invitación a {business_name} - configurá tu acceso"
    ok, detail = _send_email(staff_email, subject, html, text)
    if not ok:
        logger.error("EMAIL 3 falló a %s: %s", staff_email, detail)
    return ok, None if ok else "send_failed"


def send_password_reset_email(user_email, reset_link, business_name=""):
    """EMAIL 4: recuperación de contraseña.

    El usuario solicita reset por email y recibe un enlace de un solo uso para
    establecer una nueva contraseña. El token viaja solo en este email; la
    aplicación NUNCA loguea el token.

    Devuelve (sent: bool, reason: str|None).
    """
    business_name = business_name or "nuestro servicio"
    lifetime = platform_service_reset_lifetime()
    text = (
        f"Hola,\n\n"
        f"Recibimos una solicitud de recuperación de contraseña para {business_name}.\n"
        "Si sos vos hacé clic en el siguiente enlace para establecer una nueva contraseña:\n"
        f"{reset_link}\n\n"
        f"El enlace es de un solo uso y vence en {lifetime} hora(s).\n"
        "Si no solicitaste el cambio, ignorá este email.\n\n"
        "Saludos,\nTurnoBot"
    )
    html = (
        f"<h2>Recuperación de contraseña</h2>"
        f"<p>Hola,</p>"
        f"<p>Recibimos una solicitud de recuperación de contraseña.</p>"
        f'<p><a href="{reset_link}" '
        f'style="background:#1463FF;color:#fff;padding:10px 18px;'
        f'border-radius:8px;text-decoration:none;">Restablecer mi contraseña</a></p>'
        f"<p>El enlace es de un solo uso y vence en {lifetime} hora(s).</p>"
        f"<p>Si no solicitaste el cambio, ignorá este email.</p>"
        f"<p>Saludos,<br>TurnoBot</p>"
    )
    subject = f"Recuperación de contraseña - {business_name[:80]}"
    ok, detail = _send_email(user_email, subject, html, text)
    if not ok:
        logger.error("EMAIL 4 (reset) falló a %s: %s", user_email, detail)
    return ok, None if ok else "send_failed"


def platform_service_invitation_lifetime():
    """Import perezoso de INVITATION_LIFETIME_HOURS desde platform_service.

    Evita el import circular: notifications.py es importado por platform.py.
    """
    try:
        from services.platform import invitation_lifetime_hours

        return invitation_lifetime_hours()
    except Exception:
        return 72


def platform_service_reset_lifetime():
    """Import perezoso de PASSWORD_RESET_LIFETIME_HOURS desde platform_service.

    Evita el import circular: notifications.py es importado por platform.py.
    """
    try:
        from services.platform import PASSWORD_RESET_LIFETIME_HOURS

        return PASSWORD_RESET_LIFETIME_HOURS
    except Exception:
        return 1
