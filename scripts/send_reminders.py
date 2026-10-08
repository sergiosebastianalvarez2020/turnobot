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
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Asegurar que desde cron pueda importar el paquete del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from database.database import list_all_businesses_scoped, list_reminder_candidates_scoped
from database.pg_pool import (
    close_pg_pool,
    get_database_backend,
    init_pg_pool,
    normalize_database_url,
)
from services.notifications import notifications_enabled, send_reminder_email, smtp_configured

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("send_reminders")


def _init_cli_pool():
    """Inicializa el pool PostgreSQL para este proceso CLI y lo devuelve.

    `init_pg_pool()` sin argumentos NO alcanza en un proceso sin contexto Flask:
    `conninfo` queda en `None` y la URL solo se toma de `app.config`, asi que
    devolveria `None` aun con `DATABASE_URL` configurada y
    `get_connection()` fallaria despues con "El pool PostgreSQL no esta
    inicializado". Por eso se pasa la URL del entorno ya normalizada, igual que
    hace `database.database.get_backend()`.

    Devuelve `None` cuando el backend es SQLite (no hay nada que inicializar).
    """
    conninfo = normalize_database_url(os.getenv("DATABASE_URL"))
    pool = init_pg_pool(conninfo=conninfo)
    if pool is None and get_database_backend(conninfo) == "postgresql":
        raise RuntimeError(
            "No se pudo inicializar el pool PostgreSQL para el proceso CLI. "
            "Revisar DATABASE_URL antes de ejecutar este script."
        )
    return pool


def _local_today(timezone):
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


def _run_once():
    pool = _init_cli_pool()
    try:
        if not smtp_configured():
            logger.warning("SMTP no configurado (falta SMTP_HOST). No se enviarán recordatorios.")
            return 0

        sent = 0
        skipped = 0

        for business in list_all_businesses_scoped():
            business_id = business["id"]
            if not notifications_enabled(business_id):
                logger.info(
                    "Negocio %s (%s): notificaciones deshabilitadas.", business_id, business["slug"]
                )
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
                    logger.info("Recordatorio no enviado turno %s: %s", appointment["id"], reason)

        logger.info("Resumen: %s enviados, %s omitidos.", sent, skipped)
        return sent
    finally:
        close_pg_pool(pool)


if __name__ == "__main__":
    _run_once()
