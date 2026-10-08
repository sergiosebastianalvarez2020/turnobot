"""Helpers compartidos de la suite de integración PostgreSQL (Fase 4H).

Estas funciones NO mockean psycopg: operan exclusivamente contra conexiones
PostgreSQL reales (pool psycopg_pool o conexiones directas autocommit).
"""

import datetime
import hashlib
import re
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import psycopg
import psycopg.conninfo as _conninfo
import psycopg_pool
from psycopg import sql

from database.pg_pool import PgConnectionProxy

TURNOBOT_PG_URL_KEY = "TURNOBOT_PG_URL"
INITIAL_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "migrations_pg" / "001_initial_schema.sql"
)

# Guardia de seguridad: los nombres de base de datos de prueba de este harness
# siguen el patrón `turnobot_test_<hex>` (`new_db_name`) o `turnobot_bootstrap_<hex>`
# (usos legítimos de `init_database` en `tests_pg/test_schema_live.py`).
# Ninguna base de producción coincide con este patrón, por lo que evita que
# `CREATE DATABASE` / `DROP DATABASE ... WITH (FORCE)` actúen sobre una base
# distinta de prueba, incluso si `TURNOBOT_PG_URL` apunta a un host no controlado.
_TEST_DB_NAME_RE = re.compile(r"^turnobot_(test|bootstrap)_[0-9a-f]+$")


def _validate_test_db_name(dbname: str) -> None:
    """Rechaza nombres de base de datos que no sean de prueba.

    Previene el uso accidentario de ``create_test_database``/``drop_test_database``
    contra bases de producción o cualquier otra base no gestionada por este
    harness. La verificación se hace sobre el nombre (no sobre el host/puerto
    usuario), por lo que es compatible tanto con el entorno local (127.0.0.1:5433)
    como con GitHub Actions (postgres:16-alpine en 5432).
    """
    if not _TEST_DB_NAME_RE.fullmatch(dbname):
        raise ValueError(
            "dbname no es una base de prueba de TURNOBOT: "
            f"{dbname!r}. Solo se permiten 'turnobot_test_<hex>' o "
            "'turnobot_bootstrap_<hex>'."
        )


def connect_autocommit(conninfo: str) -> psycopg.Connection:
    """Abre una conexión PostgreSQL real en modo autocommit."""
    return psycopg.connect(conninfo, autocommit=True)


def build_test_db_conninfo(base_url: str, dbname: str) -> str:
    """Construye el conninfo de una base reemplazando el nombre de base."""
    return _conninfo.make_conninfo(base_url, dbname=dbname)


def conninfo_to_url(conninfo: str) -> str:
    """Convierte un conninfo key/value en una URL ``postgresql://``.

    Necesario para los tests que ejercitan ``init_pg_pool``/``create_app``:
    la detección de backend (igual que en producción, donde ``DATABASE_URL`` es
    una URL) se basa en el prefijo, no en el conninfo key/value que devuelve
    ``build_test_db_conninfo``.
    """
    params = _conninfo.conninfo_to_dict(conninfo)
    user = quote(params.get("user", ""), safe="")
    password = quote(params.get("password", ""), safe="")
    host = params.get("host", "localhost")
    port = params.get("port", "5432")
    dbname = params.get("dbname", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def create_test_database(base_url: str, dbname: str) -> None:
    _validate_test_db_name(dbname)
    with connect_autocommit(base_url) as conn:
        conn.execute(f"CREATE DATABASE {dbname}")


def drop_test_database(base_url: str, dbname: str) -> None:
    _validate_test_db_name(dbname)
    with connect_autocommit(base_url) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")


def apply_initial_schema(test_url: str) -> None:
    """Aplica migrations_pg/001_initial_schema.sql a la base descartable.

    El archivo contiene su propio BEGIN/COMMIT y NO es idempotente; por eso se
    aplica una única vez contra una base recién creada.

    Tras aplicar el schema se sincronizan las secuencias IDENTITY porque el propio
    archivo siembra `roles` con ids explícitos (1..4) y PostgreSQL no avanza la
    secuencia en ese caso. Es el punto común de todos los caminos del harness que
    crean una base (schema-only y schema+seed), de modo que ninguna base queda con
    secuencias desincronizadas por el `INSERT ... VALUES (id, ...)` del schema.
    """
    schema = INITIAL_SCHEMA_PATH.read_text(encoding="utf-8")
    if not schema.strip():
        raise RuntimeError(f"El archivo de schema está vacío: {INITIAL_SCHEMA_PATH}")
    with connect_autocommit(test_url) as conn:
        conn.execute(schema)
        sync_identity_sequences(conn)


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


def seed_user(url: str, email: str | None = None) -> int:
    """Crea un usuario y devuelve su id. Necesario para FK references."""
    suffix = uuid.uuid4().hex[:10]
    email = email or f"user-{suffix}@test.com"
    with connect_autocommit(url) as conn:
        row = conn.execute(
            "INSERT INTO users (email, password_hash, active) "
            "VALUES (%s, 'hash', TRUE) RETURNING id",
            (email,),
        ).fetchone()
    return row[0]


def seed_full_week(url: str, business_id: int, start: str = "09:00", end: str = "18:00") -> None:
    start_time = _as_time(start)
    end_time = _as_time(end)
    week = [(day, start_time, end_time, start_time, end_time, business_id) for day in range(7)]
    with connect_autocommit(url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
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


def sync_identity_sequences(conn) -> None:
    """Reposiciona cada secuencia IDENTITY de `public` en el MAX(id) real.

    PostgreSQL NO avanza una secuencia cuando se le inserta un id explícito, y el
    proyecto siembra así en tres lugares:

    - `migrations_pg/001_initial_schema.sql` -> `roles` (ids 1..4)
    - `seed_standard_test_data()` -> `businesses` (id 1) y `services` (ids 1..3)

    El siguiente `INSERT` sin `id` (p. ej. `provision_business()`) generaba por
    tanto un id ya ocupado y PostgreSQL respondía `duplicate key value violates
    unique constraint`. El arreglo es de raíz: en vez de un `ALTER TABLE ...
    RESTART WITH N` por test, cada secuencia se sincroniza con el máximo real de
    su columna.

    Es genérico a propósito: no conoce qué tablas se siembran ni con qué ids, así
    que sigue siendo correcto si el seed crece, se reduce o cambia de ids. Fija la
    secuencia en `MAX(id) + 1` con `is_called = false`, de modo que el siguiente
    id generado es ese valor; en una tabla vacía (`MAX(id) IS NULL`) queda en 1.
    No toca columnas sin identity.
    """
    tables = conn.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND column_name = 'id'
          AND is_identity = 'YES'
        ORDER BY table_name
        """
    ).fetchall()
    for (table_name,) in tables:
        sequence = conn.execute(
            "SELECT pg_get_serial_sequence(%s, 'id')", (f"public.{table_name}",)
        ).fetchone()[0]
        if not sequence:
            continue
        conn.execute(
            sql.SQL("SELECT setval({}, COALESCE((SELECT MAX(id) FROM {}), 0) + 1, false)").format(
                sql.Literal(sequence), sql.Identifier(table_name)
            )
        )


def seed_standard_test_data(url: str) -> int:
    """Seed the test database with standard data matching SQLite migrations.

    Creates:
    - Business with id=1 (name='El Corte', slug='el-corte')
    - Business settings for business_id=1
    - Weekly schedules for business_id=1 (matching 001_initial.sql)
    - Services for business_id=1 (Corte, Corte + barba, Barba with ids 1,2,3)

    Returns the business_id (always 1).
    """
    with connect_autocommit(url) as conn:
        # Create business with id=1
        conn.execute(
            "INSERT INTO businesses (id, name, slug, active, pending, created_at) "
            "VALUES (1, %s, %s, TRUE, FALSE, CURRENT_TIMESTAMP) "
            "ON CONFLICT (id) DO NOTHING",
            ("El Corte", "el-corte"),
        )

        # Business settings
        conn.execute(
            "INSERT INTO business_settings "
            "(business_name, slot_duration, break_between_slots, business_type, "
            " business_initials, business_description, timezone, business_id, "
            " notifications_enabled, notification_email, logo_url, "
            " primary_color, secondary_color) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (business_id) DO NOTHING",
            (
                # `002_business_configuration.sql` añade business_type/description con
                # DEFAULT acentuado y SQLite rellena la fila existente con ese valor;
                # `007` la reconstruye copiando los datos, de modo que los acentos se
                # conservan. Los DEFAULT sin acento de 007 solo aplican a filas nuevas.
                "El Corte",
                60,
                0,
                "Barbería",
                "EC",
                "Barbería masculina",
                "America/Argentina/Buenos_Aires",
                1,
                False,
                "",
                "",
                "",
                "",
            ),
        )

        # Weekly schedules (matching 001_initial.sql)
        schedules = [
            (0, True, "09:00", "13:00", "15:00", "20:00"),
            (1, True, "09:00", "13:00", "15:00", "20:00"),
            (2, True, "09:00", "13:00", "15:00", "20:00"),
            (3, True, "09:00", "13:00", "15:00", "20:00"),
            (4, True, "09:00", "13:00", "15:00", "20:00"),
            (5, True, "09:00", "13:00", None, None),
            (6, False, None, None, None, None),
        ]
        for day, is_open, morn_start, morn_end, aft_start, aft_end in schedules:
            conn.execute(
                "INSERT INTO weekly_schedules "
                "(day_of_week, is_open, morning_start, morning_end, afternoon_start, "
                " afternoon_end, business_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (business_id, day_of_week) DO NOTHING",
                (day, is_open, morn_start, morn_end, aft_start, aft_end, 1),
            )

        # Services (matching 001_initial.sql)
        services = [
            (1, "Corte", "10000", 30),
            (2, "Corte + barba", "15000", 50),
            (3, "Barba", "7000", 20),
        ]
        for svc_id, name, price, duration in services:
            conn.execute(
                "INSERT INTO services (id, name, price, duration, active, business_id) "
                "VALUES (%s, %s, %s, %s, TRUE, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (svc_id, name, price, duration, 1),
            )

        # Los ids explícitos de arriba (y los de `roles` en el schema) no mueven
        # las secuencias IDENTITY; se sincronizan una sola vez aquí.
        sync_identity_sequences(conn)

    return 1
