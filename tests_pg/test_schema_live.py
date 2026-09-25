"""Pruebas del schema PostgreSQL REAL aplicado (Fase 4H).

Verifica que migrations_pg/001_initial_schema.sql se aplica correctamente a
PostgreSQL real: conexión/versión, tablas, PK, FK, índices (incluido el único
parcial de appointments), tipos (BOOLEAN, DATE, TIME, TIMESTAMPTZ, NUMERIC) y
seed de roles. No modifica el archivo del schema.
"""

import datetime

import pytest
from psycopg.errors import UniqueViolation

from tests_pg._helpers import connect_autocommit, seed_business

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
    }
    assert expected <= indexes


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
