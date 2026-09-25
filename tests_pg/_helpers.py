"""Helpers compartidos de la suite de integración PostgreSQL (Fase 4H).

Estas funciones NO mockean psycopg: operan exclusivamente contra conexiones
PostgreSQL reales (pool psycopg_pool o conexiones directas autocommit).
"""

import datetime
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import psycopg.conninfo as _conninfo
import psycopg_pool

from database.pg_pool import PgConnectionProxy

TURNOBOT_PG_URL_KEY = "TURNOBOT_PG_URL"
INITIAL_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "migrations_pg" / "001_initial_schema.sql"
)


def connect_autocommit(conninfo: str) -> psycopg.Connection:
    """Abre una conexión PostgreSQL real en modo autocommit."""
    return psycopg.connect(conninfo, autocommit=True)


def test_db_conninfo(base_url: str, dbname: str) -> str:
    """Construye el conninfo de una base reemplazando el nombre de base."""
    return _conninfo.make_conninfo(base_url, dbname=dbname)


def create_test_database(base_url: str, dbname: str) -> None:
    with connect_autocommit(base_url) as conn:
        conn.execute(f"CREATE DATABASE {dbname}")


def drop_test_database(base_url: str, dbname: str) -> None:
    with connect_autocommit(base_url) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")


def apply_initial_schema(test_url: str) -> None:
    """Aplica migrations_pg/001_initial_schema.sql a la base descartable.

    El archivo contiene su propio BEGIN/COMMIT y NO es idempotente; por eso se
    aplica una única vez contra una base recién creada.
    """
    schema = INITIAL_SCHEMA_PATH.read_text(encoding="utf-8")
    if not schema.strip():
        raise RuntimeError(f"El archivo de schema está vacío: {INITIAL_SCHEMA_PATH}")
    with connect_autocommit(test_url) as conn:
        conn.execute(schema)


def new_db_name() -> str:
    return f"turnobot_test_{uuid.uuid4().hex}"


def make_pg_proxy(pool: psycopg_pool.ConnectionPool) -> PgConnectionProxy:
    """Checkout de una conexión REAL del pool envuelta en PgConnectionProxy."""
    return PgConnectionProxy(pool.getconn(), pool)


# ============================================================
# SEMILLAS DE DATOS (SQL directo contra PostgreSQL real)
# ============================================================


def _as_date(value: str | datetime.date) -> datetime.date:
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    return datetime.date.fromisoformat(str(value))


def _as_time(value: str | datetime.time) -> datetime.time:
    if isinstance(value, datetime.time):
        return value
    return datetime.time.fromisoformat(str(value))


def seed_business(url: str) -> int:
    suffix = uuid.uuid4().hex[:10]
    with connect_autocommit(url) as conn:
        row = conn.execute(
            "INSERT INTO businesses (name, slug, active, pending, created_at) "
            "VALUES (%s, %s, TRUE, FALSE, CURRENT_TIMESTAMP) RETURNING id",
            (f"Negocio {suffix}", f"negocio-{suffix}"),
        ).fetchone()
    return row[0]


def seed_business_settings(url: str, business_id: int) -> None:
    with connect_autocommit(url) as conn:
        conn.execute(
            "INSERT INTO business_settings "
            "(business_name, slot_duration, timezone, business_id) "
            "VALUES (%s, 30, %s, %s)",
            ("Negocio", "America/Argentina/Buenos_Aires", business_id),
        )


def seed_full_week(url: str, business_id: int, start: str = "09:00", end: str = "18:00") -> None:
    start_time = _as_time(start)
    end_time = _as_time(end)
    week = [(day, start_time, end_time, business_id) for day in range(7)]
    with connect_autocommit(url) as conn:
        conn.executemany(
            "INSERT INTO weekly_schedules "
            "(day_of_week, is_open, morning_start, morning_end, afternoon_start, "
            " afternoon_end, business_id) VALUES (%s, TRUE, %s, %s, %s, %s, %s)",
            week,
        )


def seed_service(url: str, business_id: int, name: str = "Corte", duration: int = 30) -> int:
    with connect_autocommit(url) as conn:
        row = conn.execute(
            "INSERT INTO services (name, price, duration, active, business_id) "
            "VALUES (%s, %s, %s, TRUE, %s) RETURNING id",
            (name, Decimal("100.00"), duration, business_id),
        ).fetchone()
    return row[0]


def seed_resource(url: str, business_id: int, name: str) -> int:
    with connect_autocommit(url) as conn:
        row = conn.execute(
            "INSERT INTO resources (name, active, business_id) VALUES (%s, TRUE, %s) RETURNING id",
            (name, business_id),
        ).fetchone()
    return row[0]


def seed_confirmed_appointment(
    url: str,
    business_id: int,
    token: str,
    appointment_date: str | datetime.date,
    appointment_time: str | datetime.time,
    appointment_end: str | datetime.time,
    phone: str = "5551234567",
    customer_name: str = "Ana",
    resource_id: int | None = None,
) -> int:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    appointment_date_typed = _as_date(appointment_date)
    appointment_time_typed = _as_time(appointment_time)
    appointment_end_typed = _as_time(appointment_end)
    with connect_autocommit(url) as conn:
        row = conn.execute(
            "INSERT INTO appointments "
            "(customer_name, phone, service, appointment_date, appointment_time, "
            " appointment_end, duration, status, business_id, management_token_hash, "
            " resource_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, 30, 'confirmed', %s, %s, %s) RETURNING id",
            (
                customer_name,
                phone,
                "Corte",
                appointment_date_typed,
                appointment_time_typed,
                appointment_end_typed,
                business_id,
                token_hash,
                resource_id,
            ),
        ).fetchone()
    return row[0]
