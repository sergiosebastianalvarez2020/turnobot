import datetime
import hashlib
import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_PATH = BASE_DIR / "database" / "appointments.db"

MIGRATIONS_DIR = BASE_DIR / "migrations"

logger = logging.getLogger("turnobot.db")


def get_backend():
    """Resuelve el backend de base de datos activo.

    Fuente única de verdad: `database.pg_pool.get_database_backend()`, que deriva
    del entorno (`DATABASE_URL`) — coherente con `app.config["DB_BACKEND"]` que
    `build_config` expone. Se importa perezosamente para evitar ciclos.
    """
    from database.pg_pool import get_database_backend

    return get_database_backend(os.getenv("DATABASE_URL"))


def get_connection():
    """Devuelve una conexión al backend activo.

    SQLite (default): comportamiento actual intacto — sqlite3 con row_factory,
    busy_timeout, journal_mode WAL y foreign_keys ON.

    PostgreSQL (preparado, no activo por defecto): delega en el pool existente
    de `database/pg_pool.get_pg_pool(...).getconn()`. El pool se inicializa en
    `create_app()` únicamente cuando `DB_BACKEND == "postgresql"`.

    NOTA DE COMPATIBILIDAD: psycopg3 devuelve filas por posición por defecto
    y no expone `row_factory`. Las queries SQLite usan `?` como placeholder y
    `row["col"]`. Este seam NO reescribe queries, placeholders ni migraciones.
    PostgreSQL queda preparado pero no operativo hasta una migración futura de
    queries — por eso no se activa por defecto en producción.
    """
    if get_backend() == "postgresql":
        from database.pg_pool import get_pg_pool_connection

        # PgConnectionProxy traduce connection.close() -> pool.putconn()
        # (ciclo de vida: checkout -> uso -> close -> devolución). Idempotente.
        return get_pg_pool_connection()

    # --- SQLite (default) ---
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10, check_same_thread=False)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# SISTEMA DE MIGRACIONES
# ============================================================


def _get_current_version(connection):
    try:
        row = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_version(connection, version):
    connection.execute(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)", (version,)
    )


def _migration_version(name):
    return int(name.split("_")[0])


def _ensure_migration_tables(connection):
    """Crea las tablas de control de versiones y auditoría si no existen."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL DEFAULT 0
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.commit()


def get_applied_migrations():
    """Devuelve el historial auditado de migraciones aplicadas, ordenado por versión."""
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT version, name, applied_at FROM migration_log ORDER BY version"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def apply_migrations():
    """
    Aplica migraciones pendientes en orden.

    Lee archivos .sql de migrations/ ordenados por nombre.
    Cada archivo se ejecuta con executescript (auto-commit), como es necesario
    porque algunos archivos contienen sus propios BEGIN/COMMIT.

    La versión registrada (schema_version) y la fila de auditoría (migration_log)
    se actualizan juntas y quedan en el mismo commit, de modo que un fallo no
    deja un estado intermedio entre versión y registro. La columna UNIQUE(version)
    de migration_log convierte el proceso en idempotente ante reintentos.
    """
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)

    connection = get_connection()

    try:
        _ensure_migration_tables(connection)

        current_version = _get_current_version(connection)

        all_entries = os.listdir(str(MIGRATIONS_DIR))
        migration_files = sorted(
            MIGRATIONS_DIR / name
            for name in all_entries
            if name.endswith(".sql") and name.split("_")[0].isdigit()
        )

        # Reconciliación: las migraciones ya aplicadas (versión <= actual) que
        # no tengan fila en el registro se registran retroactivamente. Así una
        # base preexistente queda completamente auditable desde el primer inicio.
        for migration_file in migration_files:
            version = _migration_version(migration_file.stem)
            if version <= current_version:
                exists = connection.execute(
                    "SELECT 1 FROM migration_log WHERE version = ?", (version,)
                ).fetchone()
                if not exists:
                    logger.info("Registrando migración ya aplicada %s...", migration_file.name)
                    connection.execute(
                        "INSERT INTO migration_log (version, name) VALUES (?, ?)",
                        (version, migration_file.name),
                    )
        connection.commit()

        for migration_file in migration_files:
            version = _migration_version(migration_file.stem)
            if version <= current_version:
                continue

            sql = migration_file.read_text(encoding="utf-8")

            logger.info("Aplicando migración %s...", migration_file.name)

            try:
                connection.executescript(sql)
                _set_version(connection, version)
                connection.execute(
                    "INSERT INTO migration_log (version, name) VALUES (?, ?)",
                    (version, migration_file.name),
                )
                connection.commit()
                logger.info("Migración %s aplicada.", migration_file.name)
            except Exception as e:
                connection.rollback()
                logger.error("ERROR en migración %s: %s", migration_file.name, e)
                raise

    finally:
        connection.close()


def init_database():
    apply_migrations()


def create_business_with_owner(name, email, password=None, slug=None):
    """Provisiona un negocio completo en una única transacción.

    Dos modos:
      - `password` presente (>=12 chars): owner inmediatamente activo
        (modo clásico, usado por scripts/provision_business.py). El negocio
        nace active=1, pending=0.
      - `password=None`: owner QUEDA PENDIENTE (active=0) con hash provisorio;
        el negocio nace PENDIENTE DE APROBACIÓN (active=0, pending=1) y su
        invitación se genera recién al aprobarlo. NO acepta IDs ni roles del
        cliente, y es una operación exclusiva de la plataforma/CLI.

    Devuelve {"business_id", "slug", "user_id", "pending": bool}.
    """
    name = name.strip() if isinstance(name, str) else ""
    email = email.strip().lower() if isinstance(email, str) else ""
    password = password if isinstance(password, str) else None
    if not 2 <= len(name) <= 120:
        raise ValueError("nombre de negocio inválido")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("email inválido")
    pending = password is None
    if not pending and len(password) < 12:
        raise ValueError("la contraseña debe tener al menos 12 caracteres")

    if slug is not None:
        slug = (slug or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
            raise ValueError("slug inválido")
    else:
        base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-") or "negocio"

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if slug is not None:
            if connection.execute("SELECT 1 FROM businesses WHERE slug = ?", (slug,)).fetchone():
                connection.rollback()
                raise ValueError("slug ya existe")
            final_slug = slug
        else:
            final_slug = base_slug
            suffix = 2
            while connection.execute(
                "SELECT 1 FROM businesses WHERE slug = ?", (final_slug,)
            ).fetchone():
                final_slug = f"{base_slug}-{suffix}"
                suffix += 1

        business_cursor = connection.execute(
            """INSERT INTO businesses (name, slug, active, pending, created_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (name, final_slug, 1 if not pending else 0, 1 if pending else 0),
        )
        business_id = business_cursor.lastrowid
        if pending:
            # Hash provisorio inutilizable: el owner aún no eligió contraseña.
            password_hash = generate_password_hash(secrets.token_urlsafe(32))
            active = 0
        else:
            password_hash = generate_password_hash(password)
            active = 1
        user_cursor = connection.execute(
            "INSERT INTO users (email, password_hash, active) VALUES (?, ?, ?)",
            (email, password_hash, active),
        )
        user_id = user_cursor.lastrowid
        owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
        connection.execute(
            "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
            (user_id, business_id, owner_role["id"]),
        )
        connection.execute(
            """INSERT INTO business_settings
               (business_name, slot_duration, break_between_slots, business_type,
                business_initials, business_description, timezone, business_id)
               VALUES (?, 60, 0, 'Negocio', ?, '', 'America/Argentina/Buenos_Aires', ?)""",
            (name, "".join(word[0] for word in name.split())[:3].upper(), business_id),
        )
        connection.execute(
            """INSERT INTO loyalty_settings
               (business_id, enabled, points_per_completed_appointment)
               VALUES (?, 0, 1)""",
            (business_id,),
        )
        for day in range(6):
            connection.execute(
                """INSERT INTO weekly_schedules
                   (day_of_week, is_open, morning_start, morning_end,
                    afternoon_start, afternoon_end, business_id)
                   VALUES (?, 1, '09:00', '13:00', '15:00', '20:00', ?)""",
                (day, business_id),
            )
        connection.execute(
            "INSERT INTO weekly_schedules (day_of_week, is_open, business_id) VALUES (6, 0, ?)",
            (business_id,),
        )
        connection.commit()
        return {
            "business_id": business_id,
            "slug": final_slug,
            "user_id": user_id,
            "pending": pending,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ============================================================
# CONSULTAS — LEGACY SINGLE-TENANT
#
# Functions below (get_active_services, get_all_services,
# create_service, update_service, get_business_settings,
# update_business_settings, get_weekly_schedule,
# update_appointment_status) are pre-multitenant leftovers that
# operate WITHOUT business_id. They are intentionally NOT removed
# (El Corte/demo and tests still depend on them), but they must
# NOT gain new callers from tenant-scoped routes. Use the
# *_scoped variants instead, which always scope by business_id.
# ============================================================


def get_active_services(business_id=None, connection=None):
    """Devuelve los servicios activos de un negocio específico."""
    if business_id is None:
        # Fallback para compatibilidad con tests existentes
        # En producción siempre se debe proveer business_id explícitamente
        fallback_connection = get_connection()
        try:
            row = fallback_connection.execute("SELECT id FROM businesses LIMIT 1").fetchone()
            if row:
                business_id = row[0]
            else:
                raise ValueError("business_id es obligatorio")
        finally:
            fallback_connection.close()

    if business_id is None:
        raise ValueError("business_id es obligatorio")

    owns_connection = connection is None
    if owns_connection:
        connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT name, price, duration
            FROM services
            WHERE active = 1 AND business_id = ?
            ORDER BY id
            """,
            (business_id,),
        ).fetchall()
    finally:
        if owns_connection:
            connection.close()


def get_all_services():
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT id, name, price, duration, active FROM services ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def create_service(name, price, duration, active=True):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO services (name, price, duration, active) VALUES (?, ?, ?, ?)",
            (name, price, duration, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_service(service_id, name, price, duration, active):
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE services
            SET name = ?, price = ?, duration = ?, active = ?
            WHERE id = ?
            """,
            (name, price, duration, 1 if active else 0, service_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_business_settings(business_id=None, connection=None):
    """Devuelve la configuración de un negocio específico."""
    if business_id is None:
        # Fallback para compatibilidad con tests existentes
        fallback_connection = get_connection()
        try:
            row = fallback_connection.execute("SELECT id FROM businesses LIMIT 1").fetchone()
            if row:
                business_id = row[0]
            else:
                raise ValueError("business_id es obligatorio")
        finally:
            fallback_connection.close()

    if business_id is None:
        raise ValueError("business_id es obligatorio")

    owns_connection = connection is None
    if owns_connection:
        connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT business_name, business_type, business_initials,
                   business_description, timezone,
                   slot_duration, break_between_slots,
                   notifications_enabled, notification_email,
                   logo_url, primary_color, secondary_color
            FROM business_settings
            WHERE business_id = ?
            """,
            (business_id,),
        ).fetchone()
    finally:
        if owns_connection:
            connection.close()


def update_business_settings(
    business_name, business_type, business_initials, business_description, timezone
):
    connection = get_connection()
    try:
        connection.execute(
            """
            UPDATE business_settings
            SET business_name = ?,
                business_type = ?,
                business_initials = ?,
                business_description = ?,
                timezone = ?
            WHERE id = 1
            """,
            (business_name, business_type, business_initials, business_description, timezone),
        )
        connection.commit()
    finally:
        connection.close()


def get_weekly_schedule(day_of_week, business_id=None, connection=None):
    if business_id is None:
        # Fallback para compatibilidad con tests existentes
        fallback_connection = get_connection()
        try:
            row = fallback_connection.execute("SELECT id FROM businesses LIMIT 1").fetchone()
            if row:
                business_id = row[0]
            else:
                raise ValueError("business_id es obligatorio")
        finally:
            fallback_connection.close()

    if business_id is None:
        raise ValueError("business_id es obligatorio")

    owns_connection = connection is None
    if owns_connection:
        connection = get_connection()
    try:
        return connection.execute(
            "SELECT * FROM weekly_schedules WHERE day_of_week = ? AND business_id = ?",
            (day_of_week, business_id),
        ).fetchone()
    finally:
        if owns_connection:
            connection.close()


def update_appointment_status(appointment_id, status):
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return False
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ?", (status, appointment_id)
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# SERVICIOS — CAPA MULTI-NEGOCIO (SCOPED)
# ============================================================


def get_active_services_scoped(business_id, connection=None):
    """Devuelve los servicios activos de un negocio específico."""
    return get_active_services(business_id, connection=connection)


def get_all_services_scoped(business_id):
    """Devuelve todos los servicios de un negocio específico."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, name, price, duration, active
            FROM services
            WHERE business_id = ?
            ORDER BY id
            """,
            (business_id,),
        ).fetchall()
    finally:
        connection.close()


def create_service_scoped(business_id, name, price, duration, active=True):
    """Crea un servicio asociado explícitamente a un negocio."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO services (business_id, name, price, duration, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (business_id, name, price, duration, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_service_scoped(service_id, business_id, name, price, duration, active):
    """Modifica un servicio solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE services
            SET name = ?, price = ?, duration = ?, active = ?
            WHERE id = ? AND business_id = ?
            """,
            (name, price, duration, 1 if active else 0, service_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# RECURSOS RESERVABLES — CAPA MULTI-NEGOCIO (SCOPED)
# ============================================================


def get_resources_scoped(business_id, only_active=False):
    """Devuelve los recursos de un negocio específico.

    Si `only_active` es True, solo devuelve los activos.
    Siempre scoped por business_id: un negocio jamás lista recursos de otro.
    """
    connection = get_connection()
    try:
        query = "SELECT id, name, active FROM resources WHERE business_id = ?"
        if only_active:
            query += " AND active = 1"
        query += " ORDER BY id"
        return connection.execute(query, (business_id,)).fetchall()
    finally:
        connection.close()


def get_resource_scoped(resource_id, business_id, connection=None):
    """Obtiene un recurso por id SOLO si pertenece al negocio indicado.

    Devuelve None si no existe o si pertenece a otro negocio.
    """
    owns_connection = connection is None
    if owns_connection:
        connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, name, active
            FROM resources
            WHERE id = ? AND business_id = ?
            """,
            (resource_id, business_id),
        ).fetchone()
    finally:
        if owns_connection:
            connection.close()


def create_resource_scoped(business_id, name, active=True):
    """Crea un recurso asociado explícitamente a un negocio.

    Devuelve el id del recurso creado.
    """
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO resources (business_id, name, active)
            VALUES (?, ?, ?)
            """,
            (business_id, name, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def set_resource_active_scoped(resource_id, business_id, active):
    """Activa o desactiva un recurso solo si pertenece al negocio indicado.

    Devuelve True si la operación modificó una fila (recurso propio),
    False si el recurso no existe o pertenece a otro negocio.
    """
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE resources
            SET active = ?
            WHERE id = ? AND business_id = ?
            """,
            (1 if active else 0, resource_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# CONFIGURACIÓN / HORARIOS / TURNOS — CAPA MULTI-NEGOCIO (SCOPED)
# ============================================================


def list_all_businesses_scoped():
    """Lista negocios con su zona horaria y flag de notificaciones.

    Usado por el runner de recordatorios para resolver "mañana" en la zona
    horaria de cada negocio sin mezclar tenants.
    """
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT b.id, b.slug, b.name,
                   bs.timezone, bs.notifications_enabled
            FROM businesses b
            LEFT JOIN business_settings bs ON bs.business_id = b.id
            ORDER BY b.id
            """
        ).fetchall()
    finally:
        connection.close()


def get_business_settings_scoped(business_id, connection=None):
    """Devuelve la configuración de un negocio específico."""
    return get_business_settings(business_id, connection=connection)


def update_business_settings_scoped(
    business_id,
    business_name,
    business_type,
    business_initials,
    business_description,
    timezone,
    notifications_enabled=None,
    notification_email=None,
    slot_duration=None,
    break_between_slots=None,
    logo_url=None,
    primary_color=None,
    secondary_color=None,
):
    """Actualiza la configuración de un negocio específico.

    `notifications_enabled`, `notification_email`, `slot_duration`,
    `break_between_slots`, `logo_url`, `primary_color` y `secondary_color` son
    opcionales: si se pasan como None, la columna correspondiente NO cambia
    (COALESCE). La operación está scoped por business_id: un tenant jamás
    modifica la configuración de otro tenant.
    """
    connection = get_connection()
    try:
        connection.execute(
            """
            UPDATE business_settings
            SET business_name = ?,
                business_type = ?,
                business_initials = ?,
                business_description = ?,
                timezone = ?,
                notifications_enabled = COALESCE(?, notifications_enabled),
                notification_email = COALESCE(?, notification_email),
                slot_duration = COALESCE(?, slot_duration),
                break_between_slots = COALESCE(?, break_between_slots),
                logo_url = COALESCE(?, logo_url),
                primary_color = COALESCE(?, primary_color),
                secondary_color = COALESCE(?, secondary_color)
            WHERE business_id = ?
            """,
            (
                business_name,
                business_type,
                business_initials,
                business_description,
                timezone,
                (1 if notifications_enabled else 0) if notifications_enabled is not None else None,
                (notification_email or "").strip() if notification_email is not None else None,
                slot_duration if slot_duration is not None else None,
                break_between_slots if break_between_slots is not None else None,
                (logo_url or "").strip() if logo_url is not None else None,
                (primary_color or "").strip() if primary_color is not None else None,
                (secondary_color or "").strip() if secondary_color is not None else None,
                business_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_all_weekly_schedules_scoped(business_id):
    """Devuelve los horarios semanales de un negocio específico, ordenados por día."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT day_of_week, is_open, morning_start, morning_end,
                   afternoon_start, afternoon_end
            FROM weekly_schedules
            WHERE business_id = ?
            ORDER BY day_of_week
            """,
            (business_id,),
        ).fetchall()
    finally:
        connection.close()


def update_weekly_schedule_scoped(
    business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end
):
    """Actualiza el horario semanal de un día específico para un negocio.

    Si is_open es False, los horarios se establecen en NULL.
    """
    connection = get_connection()
    try:
        if not is_open:
            morning_start = None
            morning_end = None
            afternoon_start = None
            afternoon_end = None
        connection.execute(
            """
            UPDATE weekly_schedules
            SET is_open = ?,
                morning_start = ?,
                morning_end = ?,
                afternoon_start = ?,
                afternoon_end = ?
            WHERE business_id = ? AND day_of_week = ?
            """,
            (
                1 if is_open else 0,
                morning_start,
                morning_end,
                afternoon_start,
                afternoon_end,
                business_id,
                day_of_week,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def set_notifications_enabled_scoped(business_id, enabled):
    """Habilita/deshabilita las notificaciones de un negocio."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE business_settings SET notifications_enabled = ? WHERE business_id = ?",
            (1 if enabled else 0, business_id),
        )
        connection.commit()
    finally:
        connection.close()


def add_notification_log_scoped(appointment_id, business_id, type_, channel, destination):
    """Registra una notificación enviada (idempotente por appointment/type/channel).

    Devuelve True si se insertó por primera vez, False si ya existía o si
    falló la integridad.
    """
    connection = get_connection()
    try:
        try:
            connection.execute(
                """
                INSERT INTO notification_log
                (appointment_id, business_id, type, channel, destination)
                VALUES (?, ?, ?, ?, ?)
                """,
                (appointment_id, business_id, type_, channel, destination),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
    finally:
        connection.close()


def notification_sent_scoped(business_id, appointment_id, type_, channel):
    """Devuelve True si ya se envió una notificación del tipo/canal indicado
    para ese turno dentro del negocio (aislamiento estricto por tenant)."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT 1 FROM notification_log
            WHERE business_id = ? AND appointment_id = ? AND type = ? AND channel = ?
            """,
            (business_id, appointment_id, type_, channel),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def get_notification_state_scoped(business_id, appointment_id, type_, channel):
    """Devuelve el estado actual de una notificación (o None).

    Estado: {"destination", "status", "error", "last_attempt_at"}.
    `status` puede ser 'pending' | 'sent' | 'failed'. Scoped por negocio.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT destination, status, error, last_attempt_at
            FROM notification_log
            WHERE business_id = ? AND appointment_id = ? AND type = ? AND channel = ?
            """,
            (business_id, appointment_id, type_, channel),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def upsert_notification_log_scoped(
    appointment_id,
    business_id,
    type_,
    channel,
    destination,
    status="sent",
    error="",
    last_attempt_at="",
):
    """Registra/actualiza una notificación de forma idempotente.

    Si ya existe el par (business_id, appointment_id, type, channel), actualiza
    destino/estado/error/último intento en lugar de fallar: así un envío fallido
    (status='failed') puede reintentarse sin duplicar filas.
    """
    connection = get_connection()
    try:
        connection.execute(
            """
            INSERT INTO notification_log
                (appointment_id, business_id, type, channel, destination,
                 status, error, last_attempt_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (business_id, appointment_id, type, channel) DO UPDATE SET
                destination = excluded.destination,
                status = excluded.status,
                error = excluded.error,
                last_attempt_at = excluded.last_attempt_at
            """,
            (
                appointment_id,
                business_id,
                type_,
                channel,
                destination,
                status,
                error[:1000],
                last_attempt_at,
            ),
        )
        connection.commit()
        return True
    except sqlite3.IntegrityError:
        connection.rollback()
        return False
    finally:
        connection.close()


def claim_notification_scoped(
    appointment_id, business_id, type_, channel, destination, stale_after_seconds=900
):
    """Claim DB atómico antes de SMTP; recupera claims abandonados."""
    connection = get_connection()
    try:
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO notification_log
               (appointment_id, business_id, type, channel, destination,
                status, error, last_attempt_at)
               VALUES (?, ?, ?, ?, ?, 'pending', '', ?)
               ON CONFLICT (business_id, appointment_id, type, channel) DO NOTHING""",
            (appointment_id, business_id, type_, channel, destination, now),
        )
        cursor = connection.execute(
            """UPDATE notification_log SET status='processing', destination=?,
                      error='', last_attempt_at=?
               WHERE business_id=? AND appointment_id=? AND type=? AND channel=?
                 AND (status IN ('pending','failed')
                      OR (status='processing' AND last_attempt_at < datetime('now', ?)))""",
            (
                destination,
                now,
                business_id,
                appointment_id,
                type_,
                channel,
                f"-{int(stale_after_seconds)} seconds",
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_failed_notifications_scoped(business_id=None, limit=100):
    """Lista notificaciones con estado 'failed' para reintento.

    Scoped por business_id (o todas si no se indica) para que un reintento
    jamás cruce el límite del tenant.
    """
    connection = get_connection()
    try:
        if business_id is not None:
            rows = connection.execute(
                """
                SELECT id, appointment_id, business_id, type, channel, destination, error
                FROM notification_log
                WHERE business_id = ? AND status = 'failed'
                ORDER BY id
                LIMIT ?
                """,
                (business_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, appointment_id, business_id, type, channel, destination, error
                FROM notification_log
                WHERE status = 'failed'
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


# ============================================================
# FIDELIZACIÓN POR PUNTOS (MVP Etapa 10.1)
#
# Esquema: loyalty_settings / loyalty_accounts / points_ledger.
# - Identidad del cliente: business_id + customer_phone normalizado (ANCLA ÚNICA).
# - El saldo es la SUM(delta) del ledger; points_balance es un cache reconstructible.
# - Todo acceso está scoped por business_id (tenant derivado del slug en la capa HTTP).
# ============================================================


def ensure_loyalty_settings_scoped(business_id):
    """Asegura que exista una fila de configuración de fidelización para el negocio.

    La fidelización arranca DESACTIVADA (enabled=0) con 1 punto por turno
    completado. Devuelve la fila como dict, o None si business_id no es válido.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM loyalty_settings WHERE business_id = ?", (business_id,)
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO loyalty_settings
                   (business_id, enabled, points_per_completed_appointment)
                   VALUES (?, 0, 1)""",
                (business_id,),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM loyalty_settings WHERE business_id = ?", (business_id,)
            ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def get_loyalty_settings_scoped(business_id):
    """Devuelve la configuración de fidelización de un negocio (o None)."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM loyalty_settings WHERE business_id = ?", (business_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def update_loyalty_settings_scoped(business_id, enabled, points_per_completed_appointment):
    """Actualiza la configuración de fidelización de un negocio (scoped).

    Valida puntos >= 0. Devuelve True si se actualizó, False si no.
    """
    try:
        points = int(points_per_completed_appointment)
    except (TypeError, ValueError):
        return False
    if points < 0:
        return False

    connection = get_connection()
    try:
        if not connection.execute(
            "SELECT 1 FROM loyalty_settings WHERE business_id = ?", (business_id,)
        ).fetchone():
            ensure_loyalty_settings_scoped(business_id)
        cursor = connection.execute(
            """UPDATE loyalty_settings
               SET enabled = ?, points_per_completed_appointment = ?, updated_at = CURRENT_TIMESTAMP
               WHERE business_id = ?""",
            (1 if enabled else 0, points, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_or_create_loyalty_account_scoped(
    business_id, customer_phone, customer_name=None, customer_email=None
):
    """Devuelve (account, created) para un cliente, creándola si no existe.

    La identidad es business_id + phone normalizado (ANCLA ÚNICA). El email y el
    nombre son auxiliares: se actualizan si se proveen y difieren, pero nunca
    determinan la clave de la cuenta. El saldo inicial siempre es 0.
    """
    phone = (customer_phone or "").strip()
    if not phone.isdigit() or len(phone) < 7:
        return None, False
    email = (customer_email or "").strip().lower() or None
    name = (customer_name or "").strip() or None

    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM loyalty_accounts WHERE business_id = ? AND customer_phone = ?",
            (business_id, phone),
        ).fetchone()
        if row is not None:
            # Actualizar auxiliares (email/name) sin tocar la identidad.
            new_email = (
                email
                if (email and (row["customer_email"] or None) != email)
                else row["customer_email"]
            )
            new_name = (
                name if (name and (row["customer_name"] or None) != name) else row["customer_name"]
            )
            if new_email != row["customer_email"] or new_name != row["customer_name"]:
                connection.execute(
                    """UPDATE loyalty_accounts
                       SET customer_email = ?, customer_name = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (new_email, new_name, row["id"]),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM loyalty_accounts WHERE id = ?", (row["id"],)
                ).fetchone()
            return dict(row), False

        inserted = connection.execute(
            """INSERT INTO loyalty_accounts
               (business_id, customer_phone, customer_email, customer_name, points_balance)
               VALUES (?, ?, ?, ?, 0)""",
            (business_id, phone, email, name),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM loyalty_accounts WHERE id = ?", (inserted.lastrowid,)
        ).fetchone()
        return (dict(row), True) if row is not None else (None, False)
    finally:
        connection.close()


def get_loyalty_account_scoped(business_id, customer_phone):
    """Devuelve la cuenta de un cliente por phone normalizado (scoped)."""
    phone = (customer_phone or "").strip()
    if not phone:
        return None
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM loyalty_accounts WHERE business_id = ? AND customer_phone = ?",
            (business_id, phone),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def get_loyalty_account_by_id_scoped(account_id, business_id):
    """Devuelve una cuenta de fidelización solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT * FROM loyalty_accounts WHERE id = ? AND business_id = ?",
            (account_id, business_id),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def list_loyalty_accounts_scoped(business_id):
    """Lista las cuentas de fidelización de un negocio ordenadas por saldo desc."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """SELECT id, business_id, customer_phone, customer_email, customer_name,
                      points_balance, created_at, updated_at
               FROM loyalty_accounts
               WHERE business_id = ?
               ORDER BY points_balance DESC, id ASC""",
            (business_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def list_points_ledger_scoped(business_id, account_id):
    """Movimientos del ledger de una cuenta del negocio (auditoría)."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """SELECT id, business_id, account_id, delta, type, reason,
                      appointment_id, points_per_completed, actor_user_id, created_at
               FROM points_ledger
               WHERE business_id = ? AND account_id = ?
               ORDER BY id ASC""",
            (business_id, account_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def insert_earn_scoped(business_id, account_id, appointment_id, points, snapshot):
    """Inserta un movimiento EARN de forma idempotente por appointment y tenant.

    Devuelve True si se insertó; False si el turno ya acreditó (IntegrityError)
    o la cantidad de puntos es <= 0. NO actualiza el saldo: lo hace el llamador
    dentro de la misma transacción.
    """
    if points <= 0:
        return False
    connection = get_connection()
    try:
        try:
            connection.execute(
                """INSERT INTO points_ledger
                   (business_id, account_id, delta, type, reason,
                    appointment_id, points_per_completed)
                   VALUES (?, ?, ?, 'earn', 'turno completado', ?, ?)""",
                (business_id, account_id, points, appointment_id, snapshot),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
    finally:
        connection.close()


def insert_adjust_scoped(business_id, account_id, delta, reason, actor_user_id):
    """Inserta un movimiento de AJUSTE ADMINISTRATIVO (+/- puntos).

    Requiere motivo y actor (admin). No permite ajustar por debajo del saldo
    disponible que ya tenga la cuenta (saldo nunca negativo). No actualiza el
    saldo: el llamador lo hace dentro de la misma transacción.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("motivo obligatorio para un ajuste de puntos")
    try:
        delta_int = int(delta)
    except (TypeError, ValueError):
        raise ValueError("cantidad de puntos inválida") from None
    if delta_int == 0:
        raise ValueError("la cantidad de puntos no puede ser cero")

    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT points_balance FROM loyalty_accounts WHERE id = ? AND business_id = ?",
            (account_id, business_id),
        ).fetchone()
        if row is None:
            raise ValueError("cuenta de fidelización no encontrada")
        if row["points_balance"] + delta_int < 0:
            raise ValueError("el saldo no puede quedar negativo")

        connection.execute(
            """INSERT INTO points_ledger
               (business_id, account_id, delta, type, reason, actor_user_id)
               VALUES (?, ?, ?, 'adjust', ?, ?)""",
            (business_id, account_id, delta_int, reason, actor_user_id),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def update_loyalty_balance_scoped(account_id, business_id):
    """Reconstruye el saldo de una cuenta (SUM del ledger) y lo actualiza.

    Usado para materializar el saldo tras un earn/adjust y en la reconciliación.
    Scoped por business_id.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            """SELECT COALESCE(SUM(delta), 0) AS total
               FROM points_ledger
               WHERE business_id = ? AND account_id = ?""",
            (business_id, account_id),
        ).fetchone()
        balance = row["total"] if row else 0
        connection.execute(
            """UPDATE loyalty_accounts
               SET points_balance = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND business_id = ?""",
            (balance, account_id, business_id),
        )
        connection.commit()
        return balance
    finally:
        connection.close()


def recalculate_all_balances_scoped(business_id):
    """Reconstruye el saldo de TODAS las cuentas de un negocio desde el ledger."""
    connection = get_connection()
    try:
        accounts = connection.execute(
            "SELECT id FROM loyalty_accounts WHERE business_id = ?", (business_id,)
        ).fetchall()
        for acc in accounts:
            row = connection.execute(
                """SELECT COALESCE(SUM(delta), 0) AS total
                   FROM points_ledger
                   WHERE business_id = ? AND account_id = ?""",
                (business_id, acc["id"]),
            ).fetchone()
            connection.execute(
                """UPDATE loyalty_accounts
                   SET points_balance = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND business_id = ?""",
                (row["total"], acc["id"], business_id),
            )
        connection.commit()
        return len(accounts)
    finally:
        connection.close()


def list_reminder_candidates_scoped(remind_date):
    """Turnos confirmados para recordar (misma fecha de turno en todas las
    zonas horarias se aproxima por fecha). Independiente de tenant para que el
    runner resuelva por negocio; el despacho usa get_business_settings_scoped.

    `remind_date` es la fecha del turno (YYYY-MM-DD).
    """
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, business_id, customer_name, customer_email, service,
                   appointment_date, appointment_time, appointment_end,
                   management_token_hash
            FROM appointments
            WHERE status = 'confirmed'
            AND appointment_date = ?
            AND customer_email IS NOT NULL
            AND customer_email != ''
            ORDER BY business_id, appointment_time
            """,
            (remind_date,),
        ).fetchall()
    finally:
        connection.close()


def get_appointment_by_token_scoped(business_id, appointment_id, management_token):
    """Devuelve un turno del negocio si el token de gestión coincide.

    Compara el SHA-256 del token provisto con el hash almacenado (nunca se
    guarda el token en claro). Devuelve fila o None.
    """
    if not management_token:
        return None
    token_hash = hashlib.sha256(management_token.encode("utf-8")).hexdigest()
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, customer_name, phone, customer_email, service,
                   appointment_date, appointment_time, appointment_end,
                   duration, status, business_id
            FROM appointments
            WHERE id = ? AND business_id = ? AND management_token_hash = ?
            """,
            (appointment_id, business_id, token_hash),
        ).fetchone()
    finally:
        connection.close()


def get_appointment_scoped(business_id, appointment_id):
    """Devuelve un turno (con estado) si pertenece al negocio indicado, o None.

    Sirve para validar efectos secundarios (p.ej. reenvíos de notificaciones)
    contra el estado real del turno sin cruzar el límite del tenant.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id, customer_name, phone, customer_email, service,
                   appointment_date, appointment_time, appointment_end,
                   duration, status, business_id
            FROM appointments
            WHERE id = ? AND business_id = ?
            """,
            (appointment_id, business_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_weekly_schedule_scoped(day_of_week, business_id, connection=None):
    """Devuelve el horario semanal de un negocio específico para un día."""
    return get_weekly_schedule(day_of_week, business_id, connection=connection)


def update_appointment_status_scoped(appointment_id, status, business_id):
    """Actualiza el estado de un turno solo si pertenece al negocio indicado."""
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return False
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ? AND business_id = ?",
            (status, appointment_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# AUTENTICACIÓN / AUTORIZACIÓN MULTINEGOCIO
#
# El tenant SIEMPRE se deriva del slug de la URL (resolve_business) y la
# autorización se comprueba contra business_users. Nunca se confía en
# business_id/role provenientes del cliente.
# ============================================================


def get_user_by_email_scoped(email):
    """Devuelve un usuario por email (con su id y estado)."""
    if not email:
        return None
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT id, email, password_hash, active FROM users WHERE email = ?", (email,)
        ).fetchone()
    finally:
        connection.close()


def get_user_by_id_scoped(user_id):
    """Devuelve un usuario por id."""
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT id, email, password_hash, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        connection.close()


def set_user_password_scoped(user_id, password_hash):
    """Actualiza el hash de contraseña de un usuario."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )
        connection.commit()
    finally:
        connection.close()


def create_user_scoped(email, password_hash, active=True):
    """Crea un usuario. Devuelve el id creado o None si el email existe."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO users (email, password_hash, active) VALUES (?, ?, ?)",
            (email, password_hash, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        connection.rollback()
        return None
    finally:
        connection.close()


def get_role_id_scoped(role_name):
    """Devuelve el id del rol por nombre."""
    connection = get_connection()
    try:
        row = connection.execute("SELECT id FROM roles WHERE name = ?", (role_name,)).fetchone()
        return row["id"] if row else None
    finally:
        connection.close()


def get_membership_scoped(user_id, business_id):
    """Devuelve la membresía (user+business+rol) de un usuario en un negocio."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT bu.user_id, bu.business_id, r.name AS role_name
            FROM business_users bu
            JOIN roles r ON r.id = bu.role_id
            WHERE bu.user_id = ? AND bu.business_id = ?
            """,
            (user_id, business_id),
        ).fetchone()
    finally:
        connection.close()


def create_membership_scoped(user_id, business_id, role_name):
    """Asocia un usuario a un negocio con un rol. Devuelve True/False."""
    role_id = get_role_id_scoped(role_name)
    if role_id is None:
        return False
    connection = get_connection()
    try:
        try:
            connection.execute(
                """
                INSERT INTO business_users (user_id, business_id, role_id)
                VALUES (?, ?, ?)
                """,
                (user_id, business_id, role_id),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
    finally:
        connection.close()


def list_members_scoped(business_id):
    """Devuelve los miembros (usuario+rol) de un negocio específico."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT bu.user_id, bu.business_id, bu.role_id, r.name AS role_name,
                   u.email, u.active
            FROM business_users bu
            JOIN roles r ON r.id = bu.role_id
            JOIN users u ON u.id = bu.user_id
            WHERE bu.business_id = ?
            ORDER BY u.email
            """,
            (business_id,),
        ).fetchall()
    finally:
        connection.close()


def change_membership_role_scoped(user_id, business_id, role_id):
    """Cambia el rol de una membresía solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE business_users
            SET role_id = ?
            WHERE user_id = ? AND business_id = ?
            """,
            (role_id, user_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        connection.rollback()
        return False
    finally:
        connection.close()


def revoke_membership_scoped(user_id, business_id):
    """Elimina una membresía solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            DELETE FROM business_users
            WHERE user_id = ? AND business_id = ?
            """,
            (user_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def count_owners_scoped(business_id):
    """Cuenta los owners (rol 'owner') de un negocio específico."""
    owner_role_id = get_role_id_scoped("owner")
    if owner_role_id is None:
        return 0
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS n
            FROM business_users
            WHERE business_id = ? AND role_id = ?
            """,
            (business_id, owner_role_id),
        ).fetchone()
        return row["n"] if row else 0
    finally:
        connection.close()


def create_session_scoped(user_id, token_hash, expires_at):
    """Crea una sesión persistente. Devuelve el id de sesión."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO sessions (user_id, token_hash, expires_at, revoked)
            VALUES (?, ?, ?, 0)
            """,
            (user_id, token_hash, expires_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def revoke_all_sessions_scoped(user_id):
    """Revoca todas las sesiones activas de un usuario (logout completo)."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,)
        )
        connection.commit()
    finally:
        connection.close()


def is_session_valid_scoped(user_id, token_hash, now_iso):
    """Devuelve True si existe una sesión activa, no revocada y no expirada."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id FROM sessions
            WHERE user_id = ?
            AND token_hash = ?
            AND revoked = 0
            AND expires_at > ?
            """,
            (user_id, token_hash, now_iso),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


# ============================================================
# PLATAFORMA / SUPERADMIN
#
# Identidad de plataforma SEPARADA de la identidad de negocio
# (users/business_users). Las funciones usan prefijo `platform_`
# para dejar explícito que NO son tenant-scoped: operan sobre la
# plataforma completa y solo las invoca el SUPERADMIN.
# ============================================================


def get_platform_user_by_email(email):
    """Devuelve un SUPERADMIN por email (sin exponer el hash puro)."""
    if not email:
        return None
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, email, display_name, password_hash, active
            FROM platform_users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()
    finally:
        connection.close()


def get_platform_user_by_id(platform_user_id):
    """Devuelve un SUPERADMIN por id."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, email, display_name, password_hash, active
            FROM platform_users
            WHERE id = ?
            """,
            (platform_user_id,),
        ).fetchone()
    finally:
        connection.close()


def create_platform_user(email, password_hash, display_name="", active=True):
    """Crea un SUPERADMIN. Devuelve el id o None si el email existe."""
    if not email:
        return None
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO platform_users (email, password_hash, display_name, active)
            VALUES (?, ?, ?, ?)
            """,
            (email.strip().lower(), password_hash, display_name, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        connection.rollback()
        return None
    finally:
        connection.close()


def list_platform_users():
    """Lista todos los SUPERADMIN (sin hash)."""
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, email, display_name, active, created_at FROM platform_users ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def set_platform_user_active(platform_user_id, active):
    """Activa/desactiva un SUPERADMIN."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE platform_users SET active = ? WHERE id = ?",
            (1 if active else 0, platform_user_id),
        )
        connection.commit()
    finally:
        connection.close()


def set_platform_user_password(platform_user_id, password_hash):
    """Actualiza el hash de contraseña de un SUPERADMIN."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE platform_users SET password_hash = ? WHERE id = ?",
            (password_hash, platform_user_id),
        )
        connection.commit()
    finally:
        connection.close()


def create_platform_session(platform_user_id, token_hash, expires_at):
    """Crea una sesión de SUPERADMIN. Devuelve el id de sesión."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO platform_sessions (platform_user_id, token_hash, expires_at, revoked)
            VALUES (?, ?, ?, 0)
            """,
            (platform_user_id, token_hash, expires_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def revoke_all_platform_sessions(platform_user_id):
    """Revoca todas las sesiones activas de un SUPERADMIN (logout)."""
    connection = get_connection()
    try:
        connection.execute(
            """
            UPDATE platform_sessions
            SET revoked = 1
            WHERE platform_user_id = ? AND revoked = 0
            """,
            (platform_user_id,),
        )
        connection.commit()
    finally:
        connection.close()


def is_platform_session_valid(platform_user_id, token_hash, now_iso):
    """True si la sesión de SUPERADMIN está activa, no revocada y sin expirar."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id FROM platform_sessions
            WHERE platform_user_id = ?
            AND token_hash = ?
            AND revoked = 0
            AND expires_at > ?
            """,
            (platform_user_id, token_hash, now_iso),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def log_platform_action(
    platform_user_id, actor_email, business_id, action, detail="", ip_address=""
):
    """Registra una acción de plataforma en audit_log (nunca secretos)."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO audit_log
            (platform_user_id, actor_email, business_id, action, detail, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (platform_user_id, (actor_email or ""), business_id, action, detail, ip_address),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def list_audit_log(limit=100):
    """Devuelve las últimas acciones de plataforma (auditoría)."""
    try:
        limit_int = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit_int = 100
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, actor_email, business_id, action, detail, ip_address, created_at
            FROM audit_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit_int,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_business_by_id_platform(business_id):
    """Devuelve un negocio (con estado active/pending) por id, uso plataforma."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id, name, slug, active, pending, created_at
            FROM businesses
            WHERE id = ?
            """,
            (business_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_businesses_for_platform():
    """Lista negocios con datos de negocio (owner, estado) para el SUPERADMIN.

    Devuelve: id, name, slug, active, pending, created_at, owner_email, member_count.
    No expone datos de clientes ni tablas de negocio.
    """
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT b.id, b.name, b.slug, b.active, b.pending, b.created_at,
                   (SELECT u.email
                    FROM business_users bu
                    JOIN roles r ON r.id = bu.role_id
                    JOIN users u ON u.id = bu.user_id
                    WHERE bu.business_id = b.id AND r.name = 'owner'
                    ORDER BY bu.id LIMIT 1) AS owner_email,
                   (SELECT COUNT(*) FROM business_users bu2
                    WHERE bu2.business_id = b.id) AS member_count
            FROM businesses b
            ORDER BY b.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def set_business_active(business_id, active):
    """Activa/desactiva un negocio (plataforma). Devuelve True si cambió."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE businesses SET active = ? WHERE id = ?", (1 if active else 0, business_id)
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def set_business_pending(business_id, pending):
    """Marca un negocio como pendiente de aprobación (o lo aprueba: pending=0).

    Devuelve True si cambió. `pending=0` significa "aprobado por superadmin".
    NOTA: no cambia `active`; el flujo de aprobación completa el estado con
    set_business_active(business_id, 1).
    """
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE businesses SET pending = ? WHERE id = ?", (1 if pending else 0, business_id)
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


# ============================================================
# INVITACIONES (establecimiento de contraseña del owner)
#
# El token SIEMPRE se guarda con hash SHA-256; el plaintext solo existe en
# el email/enlace que se le muestra al SUPERADMIN en la respuesta de alta.
# ============================================================


def create_invitation(business_id, user_id, email, role_name, token_hash, expires_at):
    """Crea una invitación pendiente. Devuelve el id creado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO invitations
            (business_id, user_id, role_name, email, token_hash, expires_at, used_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (business_id, user_id, role_name, email, token_hash, expires_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_invitation_by_token_hash(business_id, token_hash):
    """Devuelve la invitación activa de un negocio para un token dado.

    Activa = no usada, sin expirar y del negocio indicado.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT i.*, b.name AS business_name, b.slug AS business_slug,
                   u.email AS user_email
            FROM invitations i
            JOIN businesses b ON b.id = i.business_id
            JOIN users u ON u.id = i.user_id
            WHERE i.business_id = ?
            AND i.token_hash = ?
            AND i.used_at IS NULL AND i.revoked_at IS NULL
            AND i.expires_at > datetime('now')
            """,
            (business_id, token_hash),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_any_invitation_by_token_hash(token_hash):
    """Devuelve la invitación activa por token (sin filtrar negocio).

    Útil para resolver el slug desde la URL de invitación; igualmente se
    revalida contra el business_id de la ruta.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT i.*, b.name AS business_name, b.slug AS business_slug,
                   u.email AS user_email
            FROM invitations i
            JOIN businesses b ON b.id = i.business_id
            JOIN users u ON u.id = i.user_id
            WHERE i.token_hash = ?
            AND i.used_at IS NULL
            AND i.expires_at > datetime('now')
            """,
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def mark_invitation_used(invitation_id):
    """Marca la invitación como usada (timestamp actual)."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE invitations SET used_at = datetime('now') WHERE id = ?", (invitation_id,)
        )
        connection.commit()
    finally:
        connection.close()


def consume_invitation_atomically(business_id, token_hash, password_hash):
    """Consume una invitación y activa su usuario en una única transacción.

    El UPDATE condicional es el lock lógico: exactamente una conexión puede
    obtener rowcount=1 para un token vigente.
    """
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT user_id FROM invitations
               WHERE business_id = ? AND token_hash = ?
                 AND used_at IS NULL AND revoked_at IS NULL
                 AND expires_at > datetime('now')""",
            (business_id, token_hash),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        cursor = connection.execute(
            """UPDATE invitations SET used_at = datetime('now')
               WHERE business_id = ? AND token_hash = ?
                 AND used_at IS NULL AND revoked_at IS NULL
                 AND expires_at > datetime('now')""",
            (business_id, token_hash),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        cursor = connection.execute(
            "UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
            (password_hash, row["user_id"]),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        return row["user_id"]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_invitations(business_id):
    """Lista invitaciones de un negocio (plataforma), sin tokens."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, user_id, role_name, email, expires_at, used_at, revoked_at, created_at
            FROM invitations
            WHERE business_id = ?
            ORDER BY id DESC
            """,
            (business_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def revoke_active_invitations(business_id, user_id):
    connection = get_connection()
    try:
        cursor = connection.execute(
            """UPDATE invitations SET revoked_at = datetime('now')
               WHERE business_id = ? AND user_id = ?
                 AND used_at IS NULL AND revoked_at IS NULL
                 AND expires_at > datetime('now')""",
            (business_id, user_id),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def set_user_active(user_id, active):
    """Activa/desactiva un usuario de negocio (usado al aceptar invitación)."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id)
        )
        connection.commit()
    finally:
        connection.close()


# ============================================================
# PASSWORD RESET TOKENS
#
# Separados de `invitations`: los tokens de reset no tienen semántica de
# negocio/rol, aplican a cualquier usuario activo y son de un solo uso.
# El token siempre se guarda con hash SHA-256; el plaintext viaja solo en
# el email al destinatario correcto.
# ============================================================


def create_password_reset_token(user_id, token_hash, expires_at):
    """Crea un token de recuperación de contraseña. Devuelve el id creado."""
    connection = get_connection()
    try:
        revoke_previous_reset_tokens(user_id)
        cursor = connection.execute(
            """
            INSERT INTO password_reset_tokens
            (user_id, token_hash, expires_at, used_at, created_at)
            VALUES (?, ?, ?, NULL, CURRENT_TIMESTAMP)
            """,
            (user_id, token_hash, expires_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def revoke_previous_reset_tokens(user_id):
    """Marca como usados todos los tokens anteriores de un usuario (solo uno activo)."""
    connection = get_connection()
    try:
        connection.execute(
            """UPDATE password_reset_tokens
               SET used_at = datetime('now')
               WHERE user_id = ? AND used_at IS NULL""",
            (user_id,),
        )
        connection.commit()
    finally:
        connection.close()


def get_password_reset_token(token_hash):
    """Devuelve el token de reset activo por hash (no usado, sin expirar)."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT prt.*, u.email AS user_email
            FROM password_reset_tokens prt
            JOIN users u ON u.id = prt.user_id
            WHERE prt.token_hash = ?
              AND prt.used_at IS NULL
              AND prt.expires_at > datetime('now')
            """,
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def consume_password_reset_token(token_hash, password_hash):
    """Consume un token de reset, establece la contraseña y marca el token como usado.

    El UPDATE condicional es el lock lógico: exactamente una conexión puede
    obtener rowcount=1 para un token vigente. Devuelve el user_id o None.
    """
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT user_id FROM password_reset_tokens
               WHERE token_hash = ?
                 AND used_at IS NULL
                 AND expires_at > datetime('now')""",
            (token_hash,),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        cursor = connection.execute(
            """UPDATE password_reset_tokens
               SET used_at = datetime('now')
               WHERE token_hash = ?
                 AND used_at IS NULL
                 AND expires_at > datetime('now')""",
            (token_hash,),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        cursor = connection.execute(
            "UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
            (password_hash, row["user_id"]),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        return row["user_id"]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ============================================================
# STAFF INVITATIONS (reutiliza la tabla invitations)
#
# La tabla `invitations` ya soporta role_name, por lo que se reutiliza para
# invitaciones de staff/admin. La diferencia con el owner:
# - El usuario puede no existir todavía → se crea inactivo (active=0)
# - Al aceptar la invitación, se crea la membership con el rol indicado
# ============================================================


def consume_staff_invitation_atomically(business_id, token_hash, password_hash):
    """Consume una invitación de staff/admin y crea la membership.

    A diferencia de `consume_invitation_atomically` (owner), aquí:
    - Se activa el usuario (active=1)
    - Se establece la contraseña
    - Se crea la membership (business_users) con el role_name de la invitación

    Devuelve {"user_id": int, "role_name": str} o None si el token no es válido.
    """
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT user_id, role_name FROM invitations
               WHERE business_id = ? AND token_hash = ?
                 AND used_at IS NULL AND revoked_at IS NULL
                 AND expires_at > datetime('now')""",
            (business_id, token_hash),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        cursor = connection.execute(
            """UPDATE invitations SET used_at = datetime('now')
               WHERE business_id = ? AND token_hash = ?
                 AND used_at IS NULL AND revoked_at IS NULL
                 AND expires_at > datetime('now')""",
            (business_id, token_hash),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.execute(
            "UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
            (password_hash, row["user_id"]),
        )
        role_id = connection.execute(
            "SELECT id FROM roles WHERE name = ?", (row["role_name"],)
        ).fetchone()
        if role_id is None:
            connection.rollback()
            return None
        connection.execute(
            """INSERT OR IGNORE INTO business_users (user_id, business_id, role_id)
               VALUES (?, ?, ?)""",
            (row["user_id"], business_id, role_id["id"]),
        )
        connection.commit()
        return {"user_id": row["user_id"], "role_name": row["role_name"]}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
