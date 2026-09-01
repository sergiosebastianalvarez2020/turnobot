import os
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_PATH = BASE_DIR / "database" / "appointments.db"

MIGRATIONS_DIR = BASE_DIR / "migrations"


def get_connection():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        DATABASE_PATH, timeout=10, check_same_thread=False
    )
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
        row = connection.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_version(connection, version):
    connection.execute(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
        (version,),
    )


def _migration_version(name):
    return int(name.split("_")[0])


def apply_migrations():
    """
    Aplica migraciones pendientes en orden.

    Lee archivos .sql de migrations/ ordenados por nombre.
    Cada archivo se ejecuta con executescript (auto-commit).
    Si un archivo falla, se detiene y se muestra el error.
    """
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)

    connection = get_connection()

    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.commit()

        current_version = _get_current_version(connection)

        all_entries = os.listdir(str(MIGRATIONS_DIR))
        migration_files = sorted(
            MIGRATIONS_DIR / name
            for name in all_entries
            if name.endswith(".sql") and name.split("_")[0].isdigit()
        )

        pending = [
            f for f in migration_files
            if _migration_version(f.stem) > current_version
        ]

        if not pending:
            return

        for migration_file in pending:
            version = _migration_version(migration_file.stem)
            sql = migration_file.read_text(encoding="utf-8")

            print(f"  Aplicando migración {migration_file.name}...")

            try:
                connection.executescript(sql)
                _set_version(connection, version)
                connection.commit()
                print(f"  Migración {migration_file.name} aplicada.")
            except Exception as e:
                connection.rollback()
                print(f"  ERROR en migración {migration_file.name}: {e}")
                raise

    finally:
        connection.close()


def init_database():
    apply_migrations()


# ============================================================
# CONSULTAS
# ============================================================

def get_active_services():
    """Devuelve los servicios que el negocio tiene habilitados."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT name, price, duration
            FROM services
            WHERE active = 1
            ORDER BY id
            """
        ).fetchall()
    finally:
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


def get_business_settings():
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT business_name, business_type, business_initials,
                   business_description, timezone,
                   slot_duration, break_between_slots
            FROM business_settings
            WHERE id = 1
            """
        ).fetchone()
    finally:
        connection.close()


def update_business_settings(
    business_name,
    business_type,
    business_initials,
    business_description,
    timezone,
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
            (
                business_name,
                business_type,
                business_initials,
                business_description,
                timezone,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_weekly_schedule(day_of_week):
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT * FROM weekly_schedules WHERE day_of_week = ?",
            (day_of_week,),
        ).fetchone()
    finally:
        connection.close()


def update_appointment_status(appointment_id, status):
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return False
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ?",
            (status, appointment_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# SERVICIOS — CAPA MULTI-NEGOCIO (SCOPED)
# ============================================================

def get_active_services_scoped(business_id):
    """Devuelve los servicios activos de un negocio específico."""
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
        connection.close()


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
