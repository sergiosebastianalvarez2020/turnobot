"""Reintenta notificaciones por email que quedaron en estado `failed`.

Sin colas de tareas: consulta `notification_log` y re-despacha las filas
`failed` reutilizando el mismo servicio (que las actualiza a `sent` si el
envío prospera, o conserva `failed` con el error sanitizado en caso contrario).

Consistencia con el estado del turno: una fila `failed` SOLO se re-despacha si
el turno sigue existiendo y está `confirmed` en su negocio. Si el turno fue
cancelado, borrado u otro negocio, se omite (no se envían confirmaciones/
recordatorios de turnos que ya no están vigentes).

Modos:
    python scripts/retry_failed_notifications.py            # todas
    python scripts/retry_failed_notifications.py 42         # solo negocio 42
    python scripts/retry_failed_notifications.py --limit 50 # a lo sumo 50

Ejemplo de cron (cada 15 min):
    */15 * * * *  cd /ruta/al/proyecto && python scripts/retry_failed_notifications.py --limit 100

Usa la misma config SMTP que la app (env). Nunca toca la ventana de reserva.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from database.database import (
    get_appointment_scoped,
    get_business_settings_scoped,
    list_failed_notifications_scoped,
)
from services.notifications import (
    BUSINESS_CONFIRMATION,
    CONFIRMATION,
    REMINDER,
    send_appointment_notification,
    send_business_confirmation_email,
    smtp_configured,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retry_failed_notifications")


def _resend(row):
    """Re-despacha una fila `failed` según su tipo (scoped por negocio).

    El destinatario se toma del propio `notification_log` (nunca se re-apunta a
    otra dirección) y el resto del contexto se reconstruye desde el turno real.
    """
    business_id = row["business_id"]
    appointment = get_appointment_scoped(business_id, row["appointment_id"])
    if appointment is None:
        return False, "appointment_not_found"
    if appointment["status"] != "confirmed":
        return False, "appointment_not_confirmed"
    appointment["customer_email"] = row["destination"]

    if row["type"] == CONFIRMATION:
        return send_appointment_notification(
            business_id, appointment, CONFIRMATION, channel="email", force=True
        )
    if row["type"] == REMINDER:
        return send_appointment_notification(
            business_id, appointment, REMINDER, channel="email", force=True
        )
    if row["type"] == BUSINESS_CONFIRMATION:
        return send_business_confirmation_email(business_id, appointment, force=True)
    return False, "unknown_type"


def _run_once(business_id=None, limit=100):
    if not smtp_configured():
        logger.warning("SMTP no configurado (falta SMTP_HOST). No se reintentará nada.")
        return 0

    failed = list_failed_notifications_scoped(business_id, limit=limit)
    if not failed:
        logger.info("No hay notificaciones fallidas pendientes.")
        return 0

    ok_count = 0
    for row in failed:
        business_id = row["business_id"]
        settings = get_business_settings_scoped(business_id)
        if not settings:
            logger.warning("Negocio %s inexistente; se omite fila %s.", business_id, row["id"])
            continue

        ok, reason = _resend(row)
        if ok:
            ok_count += 1
            logger.info(
                "Reenviado %s turno %s (%s) -> %s",
                row["type"],
                row["appointment_id"],
                business_id,
                row["destination"],
            )
        else:
            logger.info(
                "Sigue fallando %s turno %s: %s", row["type"], row["appointment_id"], reason
            )

    logger.info("Resumen: %s reenviados de %s intentados.", ok_count, len(failed))
    return ok_count


if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:]]
    business_id = None
    limit = 100
    if args:
        if args[0] == "--limit" and len(args) > 1:
            try:
                limit = int(args[1])
            except ValueError:
                limit = 100
        else:
            try:
                business_id = int(args[0])
            except ValueError:
                business_id = None
    _run_once(business_id=business_id, limit=limit)
