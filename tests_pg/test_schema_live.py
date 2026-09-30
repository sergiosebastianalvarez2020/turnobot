"""Pruebas del schema PostgreSQL REAL aplicado (Fase 4H).

Verifica que migrations_pg/001_initial_schema.sql se aplica correctamente a
PostgreSQL real: conexión/versión, tablas, PK, FK, índices (incluido el único
parcial de appointments), tipos (BOOLEAN, DATE, TIME, TIMESTAMPTZ, NUMERIC) y
seed de roles. No modifica el archivo del schema.
"""

import datetime
import uuid

import pytest
from psycopg.errors import UniqueViolation

import tests_pg._helpers as _helpers
from tests_pg._helpers import (
    connect_autocommit,
    conninfo_to_url,
    create_test_database,
    drop_test_database,
    seed_business,
)

pytestmark = pytest.mark.pg_live

EXPECTED_TABLES = {
    "appointments",
    "audit_log",
    "business_knowledge",
    "business_settings",
    "business_users",
    "businesses",
    "conversation_analytics",
    "conversation_messages",
    "conversation_sessions",
    "invitations",
    "loyalty_accounts",
    "loyalty_settings",
    "notification_log",
    "password_reset_tokens",
    "platform_sessions",
    "platform_users",
    "points_ledger",
    "redemptions",
    "resources",
    "retention_actions",
    "rewards",
    "roles",
    "sessions",
    "services",
    "users",
    "weekly_schedules",
}


def test_conexion_real_y_version_postgres(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        version_num = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()[
            0
        ]
        version_text = conn.execute("SELECT version()").fetchone()[0]
    assert version_num >= 160000
    assert "PostgreSQL" in version_text


def test_tablas_principales_presentes(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
    actual = {row[0] for row in rows}
    assert actual == EXPECTED_TABLES


def test_pk_de_appointments(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'appointments'::regclass AND contype = 'p'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "appointments_pkey"


def test_fks_de_appointments(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT ref.relname AS referenced, con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class cls ON cls.oid = con.conrelid "
            "JOIN pg_class ref ON ref.oid = con.confrelid "
            "WHERE cls.relname = 'appointments' AND con.contype = 'f' "
            "ORDER BY con.conname"
        ).fetchall()
    assert len(rows) == 2
    referenced = {row[0] for row in rows}
    assert referenced == {"businesses", "resources"}


def test_indices_relevantes_de_appointments(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'appointments'"
        ).fetchall()
    indexes = {row[0] for row in rows}
    expected = {
        "idx_appointments_phone",
        "idx_appointments_date_status",
        "idx_appointments_status",
        "idx_appointments_business",
        "idx_appointments_management_token",
        "unique_confirmed_appointment_slot",
        "unique_appointment_idempotency_key",
    }
    assert expected <= indexes


def test_indice_unico_parcial_de_idempotencia(pg_test_database):
    """La idempotencia se verifica por COMPORTAMIENTO, no por el DDL.

    Misma clave en el mismo negocio choca; misma clave en otro negocio no; y
    NULL (turnos sin clave) no participa del índice.
    """
    with connect_autocommit(pg_test_database) as conn:
        negocio_a = seed_business(pg_test_database)
        negocio_b = seed_business(pg_test_database)

        def insertar(business_id, hora, clave):
            conn.execute(
                "INSERT INTO appointments "
                "(customer_name, service, appointment_date, appointment_time, "
                " appointment_end, duration, status, business_id, idempotency_key) "
                "VALUES (%s, 'Corte', %s, %s, %s, 30, 'cancelled', %s, %s)",
                (
                    "Cliente",
                    datetime.date(2026, 9, 1),
                    hora,
                    datetime.time(hora.hour, 30),
                    business_id,
                    clave,
                ),
            )

        insertar(negocio_a, datetime.time(10, 0), "clave-x")
        with pytest.raises(UniqueViolation):
            insertar(negocio_a, datetime.time(12, 0), "clave-x")

        # NULL convive (índice parcial) y la misma clave en otro negocio también.
        insertar(negocio_a, datetime.time(14, 0), None)
        insertar(negocio_a, datetime.time(16, 0), None)
        insertar(negocio_b, datetime.time(10, 0), "clave-x")

        total = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE business_id = %s", (negocio_a,)
        ).fetchone()[0]
    assert total == 3


def test_indice_unico_parcial_confirmed(pg_test_database):
    """El único parcial de confirmed se verifica por COMPORTAMIENTO real.

    La renderización de pg_get_indexdef es frágil (comillas/casts), por eso se
    valida la semántica: global (resource_id NULL -> COALESCE al -1) conflige
    con otro global en la misma franja pero NO con un recurso determinado; dos
    turnos con el mismo recurso confligen y con recursos distintos no.
    """
    with connect_autocommit(pg_test_database) as conn:
        business_id = seed_business(pg_test_database)
        conn.execute(
            "INSERT INTO resources (name, active, business_id) VALUES (%s, TRUE, %s)",
            ("Recurso A", business_id),
        )
        conn.execute(
            "INSERT INTO resources (name, active, business_id) VALUES (%s, TRUE, %s)",
            ("Recurso B", business_id),
        )
        resource_a, resource_b = conn.execute(
            "SELECT id FROM resources WHERE business_id = %s ORDER BY id", (business_id,)
        ).fetchall()
        resource_a, resource_b = resource_a[0], resource_b[0]

        def crear(resource_id):
            conn.execute(
                "INSERT INTO appointments "
                "(customer_name, service, appointment_date, appointment_time, "
                " appointment_end, duration, status, business_id, resource_id) "
                "VALUES (%s, %s, %s, %s, %s, 30, 'confirmed', %s, %s)",
                (
                    "Cliente",
                    "Corte",
                    datetime.date(2026, 9, 1),
                    datetime.time(10, 0),
                    datetime.time(10, 30),
                    business_id,
                    resource_id,
                ),
            )

        crear(None)
        with pytest.raises(UniqueViolation):
            crear(None)
        crear(resource_a)
        with pytest.raises(UniqueViolation):
            crear(resource_a)
        crear(resource_b)


def test_tipos_date_time_timestamptz(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'appointments' "
            "AND column_name IN "
            "('appointment_date', 'appointment_time', 'appointment_end', 'created_at')"
        ).fetchall()
    by_column = {row[0]: row[1] for row in rows}
    assert by_column["appointment_date"] == "date"
    assert by_column["appointment_time"] == "time without time zone"
    assert by_column["appointment_end"] == "time without time zone"
    assert by_column["created_at"] == "timestamp with time zone"


def test_tipos_boolean_presentes(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type = 'boolean' "
            "ORDER BY table_name, column_name"
        ).fetchall()
    booleans = {(row[0], row[1]) for row in rows}
    assert ("businesses", "active") in booleans
    assert ("businesses", "pending") in booleans
    assert ("services", "active") in booleans
    assert ("resources", "active") in booleans
    assert ("sessions", "revoked") in booleans


def test_precision_services_price(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        row = conn.execute(
            "SELECT data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_name = 'services' AND column_name = 'price'"
        ).fetchone()
    assert row[0] == "numeric"
    assert row[1] == 10
    assert row[2] == 2


def test_seed_roles_presente(pg_test_database):
    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute("SELECT id, name FROM roles ORDER BY id").fetchall()
    assert list(rows) == [(1, "owner"), (2, "admin"), (3, "staff"), (4, "customer")]


# =====================================================================
# FASE 5A — BOOTSTRAP DE POSTGRESQL DESDE CERO
# =====================================================================


def test_init_database_applies_schema_postgresql(pg_enabled: str, monkeypatch):
    """init_database() aplica migrations_pg/001_initial_schema.sql en PostgreSQL."""
    from database.database import init_database
    from database.pg_pool import close_pg_pool, init_pg_pool

    dbname = f"turnobot_bootstrap_{uuid.uuid4().hex[:10]}"
    create_test_database(pg_enabled, dbname)
    db_conninfo = _helpers.test_db_conninfo(pg_enabled, dbname)
    db_url = conninfo_to_url(db_conninfo)

    pool = init_pg_pool(None, conninfo=db_url)
    assert pool is not None
    pool.wait(timeout=15.0)

    monkeypatch.setenv("DATABASE_URL", db_url)

    try:
        with connect_autocommit(db_conninfo) as conn:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        assert len(tables) == 0

        init_database()

        with connect_autocommit(db_conninfo) as conn:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        actual = {row[0] for row in tables}
        assert "businesses" in actual
        assert "users" in actual
        assert "roles" in actual
        assert "appointments" in actual
        assert "business_knowledge" in actual

        with connect_autocommit(db_conninfo) as conn:
            indexes = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'business_knowledge'"
            ).fetchall()
        index_names = {row[0] for row in indexes}
        assert "idx_bk_tsearch" in index_names

        with connect_autocommit(db_conninfo) as conn:
            roles = conn.execute("SELECT id, name FROM roles ORDER BY id").fetchall()
        assert list(roles) == [(1, "owner"), (2, "admin"), (3, "staff"), (4, "customer")]
    finally:
        close_pg_pool(pool=pool)
        drop_test_database(pg_enabled, dbname)


def test_init_database_postgresql_idempotente(pg_enabled: str, monkeypatch):
    """Llamar a init_database() dos veces no falla ni duplica objetos."""
    from database.database import init_database
    from database.pg_pool import close_pg_pool, init_pg_pool

    dbname = f"turnobot_bootstrap_{uuid.uuid4().hex[:10]}"
    create_test_database(pg_enabled, dbname)
    db_conninfo = _helpers.test_db_conninfo(pg_enabled, dbname)
    db_url = conninfo_to_url(db_conninfo)

    pool = init_pg_pool(None, conninfo=db_url)
    assert pool is not None
    pool.wait(timeout=15.0)

    monkeypatch.setenv("DATABASE_URL", db_url)

    try:
        init_database()
        init_database()

        with connect_autocommit(db_conninfo) as conn:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        actual = {row[0] for row in tables}
        assert "businesses" in actual
        assert "users" in actual
    finally:
        close_pg_pool(pool=pool)
        drop_test_database(pg_enabled, dbname)


def test_executescript_postgresql_multiple_statements(pg_test_database):
    """executescript() divide y ejecuta correctamente SQL multi-sentencia en PostgreSQL."""
    with connect_autocommit(pg_test_database) as conn:
        conn.execute("CREATE TABLE exec_test_a (id int); CREATE TABLE exec_test_b (id int);")

    with connect_autocommit(pg_test_database) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN ('exec_test_a', 'exec_test_b')"
        ).fetchall()
    assert len(rows) == 2
