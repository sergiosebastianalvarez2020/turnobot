"""Runner de recordatorios de turnos (24 h antes).

Se ejecuta por cron una vez por día (o varias, es idempotente). Para cada
negocio calcula "mañana" en su propia zona horaria (nunca mezclando tenants)
y envía recordatorios de email a los turnos confirmados de ese día que tengan
email de contacto y aún no hayan recibido recordatorio.

Ejemplo de cron (diario a las 10:00, cada 24h antes del turno):
    0 10 * * *  cd /ruta/al/proyecto && python scripts/send_reminders.py

No requiere argumentos. Usa la misma config SMTP que la app (env).
"""

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

# Asegurar que desde cron pueda importar el paquete del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from database.database import (
    list_all_businesses_scoped,
    list_reminder_candidates_scoped,
)
from services.notifications import (
    notifications_enabled,
    send_reminder_email,
    smtp_configured,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("send_reminders")


def _local_today(timezone):
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


def _run_once():
    if not smtp_configured():
        logger.warning(
            "SMTP no configurado (falta SMTP_HOST). No se enviarán recordatorios."
        )
        return 0

    sent = 0
    skipped = 0

    for business in list_all_businesses_scoped():
        business_id = business["id"]
        if not notifications_enabled(business_id):
            logger.info("Negocio %s (%s): notificaciones deshabilitadas.", business_id, business["slug"])
            continue

        tomorrow = _local_today(business["timezone"])
        candidates = list_reminder_candidates_scoped(tomorrow.strftime("%Y-%m-%d"))

        for appointment in candidates:
            # list_reminder_candidates_scoped ya filtra por fecha; seguro acotar
            # por negocio acá para respetar estrictamente la separación de tenants.
            if appointment["business_id"] != business_id:
                continue

            ok, reason = send_reminder_email(business_id, dict(appointment))
            if ok:
                sent += 1
                logger.info(
                    "Recordatorio enviado: turno %s (%s) -> %s",
                    appointment["id"],
                    appointment["appointment_time"],
                    appointment["customer_email"],
                )
            else:
                skipped += 1
                logger.info(
                    "Recordatorio no enviado turno %s: %s",
                    appointment["id"],
                    reason,
                )

    logger.info("Resumen: %s enviados, %s omitidos.", sent, skipped)
    return sent


if __name__ == "__main__":
    _run_once()
