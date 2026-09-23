"""Aviso por email ante el fallo de una unidad systemd de TurnoBot.

Pensado para ser disparado por ``OnFailure=`` desde las unidades de backup y
verificación. Usa la configuración SMTP del entorno (``/etc/turnobot.env``) y
NO depende de la configuración de negocio (``notifications_enabled`` /
``notification_email``).

Uso:
    python scripts/notify_failure.py <nombre-unidad>

Nunca imprime ni envía la contraseña SMTP. Si el SMTP no está configurado,
informa por stderr y termina sin fallar.
"""

import os
import smtplib
import socket
import ssl
import sys
from datetime import UTC, datetime
from email.message import EmailMessage


def _smtp_configured():
    return bool(os.environ.get("SMTP_HOST"))


def _recipient():
    return os.environ.get("ALERT_EMAIL") or os.environ.get("EMAIL_FROM", "")


def _build_message(unit):
    sender = os.environ.get("EMAIL_FROM", "")
    sender_name = os.environ.get("EMAIL_FROM_NAME", "").strip()
    recipient = _recipient()

    message = EmailMessage()
    message["Subject"] = f"[TurnoBot] Fallo en {unit}"
    message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = recipient
    message.set_content(
        f"La unidad systemd '{unit}' terminó con fallo.\n\n"
        f"Host: {socket.gethostname()}\n"
        f"Fecha (UTC): {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Revisar el journal:\n"
        f"    sudo journalctl -u {unit} -n 100 --no-pager\n"
    )
    return message


def send_failure_notice(unit):
    if not _smtp_configured():
        print("SMTP no configurado (falta SMTP_HOST); no se envía aviso.", file=sys.stderr)
        return False

    recipient = _recipient()
    if not recipient:
        print("Sin destinatario (EMAIL_FROM/ALERT_EMAIL); no se envía aviso.", file=sys.stderr)
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    message = _build_message(unit)

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        try:
            server.ehlo()
            if port != 465 and (port == 587 or user):
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if user:
                server.login(user, password)
            server.send_message(message)
        finally:
            server.quit()
    except Exception as error:  # noqa: BLE001 - se reporta sin filtrar secretos
        print(f"No se pudo enviar el aviso de fallo: {error}", file=sys.stderr)
        return False

    print(f"Aviso de fallo enviado ({unit}) -> {recipient}")
    return True


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    unit = argv[0] if argv else "(unidad desconocida)"
    return 0 if send_failure_notice(unit) else 1


if __name__ == "__main__":
    raise SystemExit(main())
