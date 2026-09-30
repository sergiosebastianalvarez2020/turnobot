"""Tests del migrador SQLite → PostgreSQL (Fase 5A).

Cubiertas:
    A. Migración de una base SQLite pequeña hacia PostgreSQL.
    B. Preservación de IDs.
    C. Preservación de foreign keys.
    D. Conversión SQLite BOOLEAN → PostgreSQL BOOLEAN.
    E. Conversión DATE/TIME/TIMESTAMP.
    F. NULL.
    G. Sequences ajustadas correctamente.
    H. Columnas GENERATED de PostgreSQL no reciben INSERT directo.
    I. FTS PostgreSQL funciona después de migrar knowledge.
    J. Rollback cuando una tabla falla.
    K. DRY-RUN no modifica PostgreSQL.
    L. Segunda ejecución: se rechaza explícitamente BD destino con datos.
    M. Integración end-to-end con dataset representativo.
"""

import datetime
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from database.migrator import (
    MigrationError,
    MigrationTargetNotEmptyError,
    migrate_sqlite_to_postgres,
)
from tests_pg._helpers import (
    apply_initial_schema,
    connect_autocommit,
    conninfo_to_url,
    create_test_database,
    drop_test_database,
    new_db_name,
    build_test_db_conninfo,
)

pytestmark = pytest.mark.pg_live


def _create_sqlite_test_db() -> tuple[Path, Any]:
    """Crea una base SQLite pequeña con datos para migrar.

    Returns: (db_path, temp_dir_obj) where temp_dir_obj is a TemporaryDirectory.
    """
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "test_migration.db"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            pending INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        INSERT INTO roles (id, name) VALUES (1, 'owner'), (2, 'admin'), (3, 'staff'), (4, 'customer');

        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE business_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT DEFAULT 'Mi negocio',
            slot_duration INTEGER NOT NULL DEFAULT 60,
            break_between_slots INTEGER NOT NULL DEFAULT 0,
            business_type TEXT NOT NULL DEFAULT 'Barberia',
            business_initials TEXT NOT NULL DEFAULT 'EC',
            business_description TEXT NOT NULL DEFAULT 'Barberia masculina',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            business_id INTEGER NOT NULL UNIQUE,
            notifications_enabled INTEGER NOT NULL DEFAULT 0,
            notification_email TEXT NOT NULL DEFAULT '',
            logo_url TEXT NOT NULL DEFAULT '',
            primary_color TEXT NOT NULL DEFAULT '',
            secondary_color TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE weekly_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week INTEGER NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 0,
            morning_start TEXT,
            morning_end TEXT,
            afternoon_start TEXT,
            afternoon_end TEXT,
            business_id INTEGER NOT NULL,
            UNIQUE (business_id, day_of_week)
        );

        CREATE TABLE services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            duration INTEGER NOT NULL DEFAULT 60,
            active INTEGER NOT NULL DEFAULT 1,
            business_id INTEGER NOT NULL
        );

        CREATE TABLE appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            phone TEXT,
            customer_email TEXT,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            appointment_end TEXT NOT NULL,
            duration INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            business_id INTEGER NOT NULL,
            management_token_hash TEXT,
            resource_id INTEGER,
            idempotency_key TEXT
        );

        CREATE TABLE business_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            business_id INTEGER NOT NULL DEFAULT 1,
            role_id INTEGER NOT NULL,
            UNIQUE (user_id, business_id)
        );

        CREATE TABLE business_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            tags TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by_user_id INTEGER
        );
        """
    )

    conn.execute(
        "INSERT INTO businesses (id, name, slug, active, pending, created_at) "
        "VALUES (1, 'Negocio Test', 'test-slug', 1, 0, '2026-01-15 10:30:00')"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, active, created_at) "
        "VALUES (1, 'owner@test.com', 'hash123', 1, '2026-01-15 10:30:00')"
    )
    conn.execute(
        "INSERT INTO business_settings (id, business_name, slot_duration, business_id, "
        "notifications_enabled, notification_email) VALUES "
        "(1, 'Negocio Test', 30, 1, 1, 'admin@test.com')"
    )
    conn.execute(
        "INSERT INTO weekly_schedules (id, day_of_week, is_open, morning_start, morning_end, "
        "afternoon_start, afternoon_end, business_id) VALUES "
        "(1, 0, 1, '09:00', '13:00', '15:00', '20:00', 1)"
    )
    conn.execute(
        "INSERT INTO services (id, name, price, duration, active, business_id) "
        "VALUES (1, 'Corte', 100.50, 30, 1, 1)"
    )
    conn.execute(
        "INSERT INTO business_users (id, user_id, business_id, role_id) VALUES (1, 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO appointments (id, customer_name, phone, customer_email, service, "
        "appointment_date, appointment_time, appointment_end, duration, status, "
        "created_at, business_id, management_token_hash, resource_id) "
        "VALUES (1, 'Ana', '5551234', 'ana@test.com', 'Corte', "
        "'2026-06-15', '10:00', '10:30', 30, 'confirmed', '2026-06-01 09:00:00', 1, 'hash_token', NULL)"
    )
    conn.execute(
        "INSERT INTO business_knowledge (id, business_id, type, question, answer, tags, active, "
        "created_at, updated_at, created_by_user_id) "
        "VALUES (1, 1, 'faq', 'Cuanto cuesta el corte', '100 pesos', 'precio,corte', 1, "
        "'2026-01-15 10:30:00', '2026-01-15 10:30:00', 1)"
    )
    conn.commit()
    conn.close()

    return db_path, temp_dir


def _setup_pg_target(pg_enabled: str) -> tuple[str, str, str]:
    """Crea una base PG descartable, aplica el schema y devuelve (dbname, conninfo, url)."""
    dbname = new_db_name()
    create_test_database(pg_enabled, dbname)
    db_conninfo = build_test_db_conninfo(pg_enabled, dbname)
    db_url = conninfo_to_url(db_conninfo)
    apply_initial_schema(db_url)
    return dbname, db_conninfo, db_url


# =====================================================================
# A. Migración de una base SQLite pequeña hacia PostgreSQL.
# =====================================================================


def test_migrate_small_sqlite_to_postgres(pg_enabled):
    """A. Migración completa de una base SQLite pequeña."""
    db_path, temp_dir = _create_sqlite_test_db()

    dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

    try:
        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success
        assert report.total_rows_read > 0
        assert report.total_rows_inserted > 0

        with connect_autocommit(db_conninfo) as conn:
            biz = conn.execute("SELECT id, name, slug FROM businesses WHERE id = 1").fetchone()
            assert biz is not None
            assert biz[0] == 1
            assert biz[1] == "Negocio Test"

            apps = conn.execute(
                "SELECT id, customer_name, phone, customer_email FROM appointments WHERE id = 1"
            ).fetchone()
            assert apps is not None
            assert apps[0] == 1
            assert apps[1] == "Ana"

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# B. Preservación de IDs.
# =====================================================================


def test_preserve_ids(pg_enabled):
    """B. Los IDs de origen se preservan en PostgreSQL."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            biz = conn.execute("SELECT id FROM businesses WHERE id = 1").fetchone()
            assert biz is not None

            user = conn.execute("SELECT id FROM users WHERE id = 1").fetchone()
            assert user is not None

            appt = conn.execute("SELECT id FROM appointments WHERE id = 1").fetchone()
            assert appt is not None

            bk = conn.execute("SELECT id FROM business_knowledge WHERE id = 1").fetchone()
            assert bk is not None

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# C. Preservación de foreign keys.
# =====================================================================


def test_foreign_keys_preserved(pg_enabled):
    """C. Las foreign keys referencian los IDs correctos."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            bu = conn.execute(
                "SELECT user_id, business_id, role_id FROM business_users WHERE id = 1"
            ).fetchone()
            assert bu is not None
            assert bu[0] == 1
            assert bu[1] == 1
            assert bu[2] == 1

            appt = conn.execute("SELECT business_id FROM appointments WHERE id = 1").fetchone()
            assert appt is not None
            assert appt[0] == 1

            bk = conn.execute("SELECT business_id FROM business_knowledge WHERE id = 1").fetchone()
            assert bk is not None
            assert bk[0] == 1

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# D. Conversión SQLite BOOLEAN → PostgreSQL BOOLEAN.
# =====================================================================


def test_boolean_conversion(pg_enabled):
    """D. Los flags 0/1 de SQLite se convierten a BOOLEAN en PostgreSQL."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            biz = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'businesses' AND column_name = 'active'"
            ).fetchone()
            assert biz[0] == "boolean"

            active_val = conn.execute("SELECT active FROM businesses WHERE id = 1").fetchone()
            assert active_val[0] is True

            kb_active = conn.execute(
                "SELECT active FROM business_knowledge WHERE id = 1"
            ).fetchone()
            assert kb_active[0] is True

            sched = conn.execute("SELECT is_open FROM weekly_schedules WHERE id = 1").fetchone()
            assert sched[0] is True

            svc = conn.execute("SELECT active FROM services WHERE id = 1").fetchone()
            assert svc[0] is True

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# E. Conversión DATE/TIME/TIMESTAMP.
# =====================================================================


def test_date_time_timestamp_conversion(pg_enabled):
    """E. Las fechas y horas se insertan correctamente en tipos PG."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            date_type = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'appointments' AND column_name = 'appointment_date'"
            ).fetchone()[0]
            assert date_type == "date"

            appt_date = conn.execute(
                "SELECT appointment_date FROM appointments WHERE id = 1"
            ).fetchone()[0]
            assert appt_date == datetime.date(2026, 6, 15)

            time_type = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'appointments' AND column_name = 'appointment_time'"
            ).fetchone()[0]
            assert time_type == "time without time zone"

            appt_time = conn.execute(
                "SELECT appointment_time FROM appointments WHERE id = 1"
            ).fetchone()[0]
            assert appt_time == datetime.time(10, 0)

            ts_type = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'appointments' AND column_name = 'created_at'"
            ).fetchone()[0]
            assert ts_type == "timestamp with time zone"

            created_at = conn.execute(
                "SELECT created_at FROM appointments WHERE id = 1"
            ).fetchone()[0]
            assert created_at == datetime.datetime(2026, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# F. NULL.
# =====================================================================


def test_null_values_preserved(pg_enabled):
    """F. NULL en SQLite se preserva como NULL en PostgreSQL."""
    db_path, temp_dir = _create_sqlite_test_db()

    sqlite_conn = sqlite3.connect(str(db_path))
    sqlite_conn.execute(
        "INSERT INTO appointments (id, customer_name, phone, customer_email, service, "
        "appointment_date, appointment_time, appointment_end, duration, "
        "status, created_at, business_id, management_token_hash) "
        "VALUES (2, 'Bob', NULL, NULL, 'Corte', '2026-06-16', '11:00', '11:30', 30, "
        "'confirmed', '2026-06-01 09:00:00', 1, 'hash2')"
    )
    sqlite_conn.commit()
    sqlite_conn.close()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            phone = conn.execute("SELECT phone FROM appointments WHERE id = 2").fetchone()[0]
            assert phone is None

            email = conn.execute("SELECT customer_email FROM appointments WHERE id = 2").fetchone()[
                0
            ]
            assert email is None

            resource = conn.execute("SELECT resource_id FROM appointments WHERE id = 1").fetchone()[
                0
            ]
            assert resource is None

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# G. Sequences ajustadas correctamente.
# =====================================================================


def test_sequences_adjusted_after_migration(pg_enabled):
    """G. Las sequences de PG se ajustan al MAX(id) tras migrar."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            biz_id = conn.execute(
                "INSERT INTO businesses (name, slug, active, pending) "
                "VALUES ('Nuevo', 'nuevo-slug', TRUE, FALSE) RETURNING id"
            ).fetchone()[0]
            assert biz_id == 2

            appt_id = conn.execute(
                "INSERT INTO appointments (customer_name, service, appointment_date, "
                "appointment_time, appointment_end, duration, business_id) "
                "VALUES ('Test', 'Corte', '2026-07-01', '10:00', '10:30', 30, 1) RETURNING id"
            ).fetchone()[0]
            assert appt_id == 2

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# H. Columnas GENERATED no reciben INSERT directo.
# =====================================================================


def test_generated_columns_excluded(pg_enabled):
    """H. La columna document_tsearch (GENERATED) se excluye del INSERT."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        assert report.success

        with connect_autocommit(db_conninfo) as conn:
            gen_check = conn.execute(
                "SELECT column_name, is_generated "
                "FROM information_schema.columns "
                "WHERE table_name = 'business_knowledge' AND column_name = 'document_tsearch'"
            ).fetchone()
            assert gen_check is not None
            assert gen_check[1] == "ALWAYS"

            tsearch_val = conn.execute(
                "SELECT document_tsearch IS NOT NULL FROM business_knowledge WHERE id = 1"
            ).fetchone()[0]
            assert tsearch_val is True

            result = next(r for r in report.table_results if r.table_name == "business_knowledge")
            assert result.error is None

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# I. FTS PostgreSQL funciona después de migrar knowledge.
# =====================================================================


def test_fts_works_after_migration(pg_enabled):
    """I. La búsqueda full-text PostgreSQL funciona tras migrar business_knowledge."""
    from database.pg_pool import close_pg_pool, init_pg_pool
    from services.knowledge import search_knowledge_scoped
    from tests_pg._helpers import make_pg_proxy

    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, db_url = _setup_pg_target(pg_enabled)
        pool = init_pg_pool(None, conninfo=db_url)

        try:
            report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

            assert report.success

            with patch("services.knowledge.get_connection", lambda: make_pg_proxy(pool)):
                results = search_knowledge_scoped(1, "corte", limit=5)
                assert len(results) >= 1
                assert any("corte" in r["question"].lower() for r in results)

                results = search_knowledge_scoped(1, "precio", limit=5)
                assert len(results) >= 1
        finally:
            close_pg_pool(pool=pool)

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# J. Rollback cuando una tabla falla.
# =====================================================================


def test_rollback_on_failure(pg_enabled):
    """J. Si una tabla falla, PostgreSQL se revierte completamente (rollback)."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        sqlite_conn = sqlite3.connect(str(db_path))
        sqlite_conn.execute(
            "INSERT INTO appointments (id, customer_name, phone, customer_email, service, "
            "appointment_date, appointment_time, appointment_end, duration, "
            "status, created_at, business_id) "
            "VALUES (99, 'Bad', NULL, NULL, 'Test', '2026-07-01', '10:00', '10:30', 30, "
            "'confirmed', '2026-07-01 09:00:00', 999)"
        )
        sqlite_conn.commit()
        sqlite_conn.close()

        custom_order = ["businesses", "users", "appointments", "business_knowledge"]

        with pytest.raises(MigrationError):
            migrate_sqlite_to_postgres(
                str(db_path), db_conninfo, dry_run=False, table_order=custom_order
            )

        with connect_autocommit(db_conninfo) as conn:
            biz_count = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
            assert biz_count == 0, "La migración fue revertida; businesses debe estar vacío"

        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)
    except Exception:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)
        raise


# =====================================================================
# K. DRY-RUN no modifica PostgreSQL.
# =====================================================================


def test_dry_run_does_not_modify(pg_enabled):
    """K. El modo DRY-RUN no inserta nada en PostgreSQL."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=True)

        assert report.dry_run
        assert report.success
        assert report.total_rows_read > 0

        with connect_autocommit(db_conninfo) as conn:
            biz_count = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
            assert biz_count == 0, "DRY-RUN no debe insertar datos"

            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            assert user_count == 0

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# L. Segunda ejecución: rechaza BD destino con datos.
# =====================================================================


def test_rejects_non_empty_target(pg_enabled):
    """L. Si la BD destino ya tiene datos, la migración se rechaza explícitamente."""
    db_path, temp_dir = _create_sqlite_test_db()

    try:
        dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

        report1 = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)
        assert report1.success

        with pytest.raises(MigrationTargetNotEmptyError):
            migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


# =====================================================================
# M. Integración end-to-end con dataset representativo.
# =====================================================================


def _create_representative_sqlite_db() -> tuple[Path, Any]:
    """Crea una base SQLite representativa con datos multi-tabla + multi-tenant.

    Tablas y datos:
      - businesses: 2 empresas (id=1,2)
      - roles: seed (1=owner, 2=admin, 3=staff, 4=customer)
      - users: 3 usuarios (id=1,2,3)
      - business_settings: configuración para ambas empresas (id=1,2)
      - weekly_schedules: horarios para ambas empresas (id=1,2,3,4)
      - services: 3 servicios (id=1,2,3)
      - appointments: 3 turnos (id=1,2,3) con NULLs y FK válidas
      - business_users: 2 relaciones (id=1,2)
      - business_knowledge: 4 ítems de conocimiento (id=1,2,3,4)
      - loyalty_settings + loyalty_accounts: 1 cada uno
      - rewards: 1 reward
      - retention_actions: 1 acción

    Returns: (db_path, temp_dir_obj)
    """
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "representative_migration.db"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            pending INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        INSERT INTO roles (id, name) VALUES (1, 'owner'), (2, 'admin'), (3, 'staff'), (4, 'customer');

        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE business_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT DEFAULT 'Mi negocio',
            slot_duration INTEGER NOT NULL DEFAULT 60,
            break_between_slots INTEGER NOT NULL DEFAULT 0,
            business_type TEXT NOT NULL DEFAULT 'Barberia',
            business_initials TEXT NOT NULL DEFAULT 'EC',
            business_description TEXT NOT NULL DEFAULT 'Barberia masculina',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            business_id INTEGER NOT NULL UNIQUE,
            notifications_enabled INTEGER NOT NULL DEFAULT 0,
            notification_email TEXT NOT NULL DEFAULT '',
            logo_url TEXT NOT NULL DEFAULT '',
            primary_color TEXT NOT NULL DEFAULT '',
            secondary_color TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE weekly_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week INTEGER NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 0,
            morning_start TEXT,
            morning_end TEXT,
            afternoon_start TEXT,
            afternoon_end TEXT,
            business_id INTEGER NOT NULL,
            UNIQUE (business_id, day_of_week)
        );

        CREATE TABLE services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            duration INTEGER NOT NULL DEFAULT 60,
            active INTEGER NOT NULL DEFAULT 1,
            business_id INTEGER NOT NULL
        );

        CREATE TABLE appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            phone TEXT,
            customer_email TEXT,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            appointment_end TEXT NOT NULL,
            duration INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            business_id INTEGER NOT NULL,
            management_token_hash TEXT,
            resource_id INTEGER,
            idempotency_key TEXT
        );

        CREATE TABLE resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE (business_id, name)
        );

        CREATE TABLE business_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            business_id INTEGER NOT NULL DEFAULT 1,
            role_id INTEGER NOT NULL,
            UNIQUE (user_id, business_id)
        );

        CREATE TABLE business_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            tags TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by_user_id INTEGER
        );

        CREATE TABLE loyalty_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 0,
            points_per_completed_appointment INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE loyalty_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            customer_phone TEXT NOT NULL,
            customer_email TEXT,
            customer_name TEXT,
            points_balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (business_id, customer_phone)
        );

        CREATE TABLE rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            points_cost INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (business_id, name)
        );

        CREATE TABLE retention_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            customer_phone TEXT NOT NULL,
            action_type TEXT NOT NULL,
            notes TEXT,
            actor_user_id INTEGER,
            last_completed_date DATE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # businesses (id=1,2)
    conn.execute(
        "INSERT INTO businesses (id, name, slug, active, pending, created_at) VALUES "
        "(1, 'Barberia Principal', 'barberia-1', 1, 0, '2026-01-15 10:30:00')"
    )
    conn.execute(
        "INSERT INTO businesses (id, name, slug, active, pending, created_at) VALUES "
        "(2, 'Estudio 2', 'estudio-2', 1, 0, '2026-03-20 08:00:00')"
    )

    # users (id=1,2,3)
    conn.execute(
        "INSERT INTO users (id, email, password_hash, active, created_at) VALUES "
        "(1, 'owner@barberia1.com', 'hash1', 1, '2026-01-15 10:30:00')"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, active, created_at) VALUES "
        "(2, 'admin@barberia1.com', 'hash2', 1, '2026-02-01 12:00:00')"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, active, created_at) VALUES "
        "(3, 'cliente@estudio2.com', 'hash3', 0, '2026-03-20 08:00:00')"
    )

    # business_settings (id=1,2)
    conn.execute(
        "INSERT INTO business_settings (id, business_name, slot_duration, business_id, "
        "notifications_enabled, notification_email) VALUES "
        "(1, 'Barberia Principal', 30, 1, 1, 'admin@barberia1.com')"
    )
    conn.execute(
        "INSERT INTO business_settings (id, business_name, slot_duration, business_id, "
        "notifications_enabled, notification_email) VALUES "
        "(2, 'Estudio 2', 45, 2, 0, 'info@estudio2.com')"
    )

    # weekly_schedules (id=1,2,3,4)
    conn.execute(
        "INSERT INTO weekly_schedules (id, day_of_week, is_open, morning_start, morning_end, "
        "afternoon_start, afternoon_end, business_id) VALUES "
        "(1, 0, 1, '09:00', '13:00', '15:00', '20:00', 1)"
    )
    conn.execute(
        "INSERT INTO weekly_schedules (id, day_of_week, is_open, morning_start, morning_end, "
        "afternoon_start, afternoon_end, business_id) VALUES "
        "(2, 1, 1, '09:00', '13:00', '15:00', '20:00', 1)"
    )
    conn.execute(
        "INSERT INTO weekly_schedules (id, day_of_week, is_open, morning_start, morning_end, "
        "afternoon_start, afternoon_end, business_id) VALUES "
        "(3, 0, 1, '10:00', '14:00', '16:00', '21:00', 2)"
    )
    conn.execute(
        "INSERT INTO weekly_schedules (id, day_of_week, is_open, morning_start, morning_end, "
        "afternoon_start, afternoon_end, business_id) VALUES "
        "(4, 2, 0, NULL, NULL, NULL, NULL, 2)"
    )

    # services (id=1,2,3)
    conn.execute(
        "INSERT INTO services (id, name, price, duration, active, business_id) VALUES "
        "(1, 'Corte Hombre', 100.50, 30, 1, 1)"
    )
    conn.execute(
        "INSERT INTO services (id, name, price, duration, active, business_id) VALUES "
        "(2, 'Corte Mujer', 150.00, 45, 1, 1)"
    )
    conn.execute(
        "INSERT INTO services (id, name, price, duration, active, business_id) VALUES "
        "(3, 'Recogida', 80.00, 20, 0, 2)"
    )

    # resources (id=1)
    conn.execute(
        "INSERT INTO resources (id, business_id, name, active) VALUES (1, 1, 'Silla 1', 1)"
    )

    # appointments (id=1,2,3) - incluye NULLs
    conn.execute(
        "INSERT INTO appointments (id, customer_name, phone, customer_email, service, "
        "appointment_date, appointment_time, appointment_end, duration, status, "
        "created_at, business_id, management_token_hash, resource_id, idempotency_key) "
        "VALUES (1, 'Ana', '5551234', 'ana@test.com', 'Corte Hombre', "
        "'2026-06-15', '10:00', '10:30', 30, 'confirmed', '2026-06-01 09:00:00', 1, "
        "'token_hash_1', NULL, 'key-001')"
    )
    conn.execute(
        "INSERT INTO appointments (id, customer_name, phone, customer_email, service, "
        "appointment_date, appointment_time, appointment_end, duration, status, "
        "created_at, business_id, management_token_hash, resource_id, idempotency_key) "
        "VALUES (2, 'Bob', NULL, NULL, 'Corte Mujer', '2026-06-16', '11:00', '11:45', 45, "
        "'pending', '2026-06-02 14:30:00', 1, 'token_hash_2', 1, NULL)"
    )
    conn.execute(
        "INSERT INTO appointments (id, customer_name, phone, customer_email, service, "
        "appointment_date, appointment_time, appointment_end, duration, status, "
        "created_at, business_id, management_token_hash, resource_id, idempotency_key) "
        "VALUES (3, 'Carlos', '5559999', 'carlos@test.com', 'Recogida', "
        "'2026-06-17', '15:00', '15:20', 20, 'confirmed', '2026-06-03 10:00:00', 2, "
        "'token_hash_3', NULL, 'key-003')"
    )

    # business_users (id=1,2)
    conn.execute(
        "INSERT INTO business_users (id, user_id, business_id, role_id) VALUES (1, 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO business_users (id, user_id, business_id, role_id) VALUES (2, 2, 1, 2)"
    )

    # business_knowledge (id=1,2,3,4) - para FTS
    conn.execute(
        "INSERT INTO business_knowledge (id, business_id, type, question, answer, tags, "
        "active, created_at, updated_at, created_by_user_id) VALUES "
        "(1, 1, 'faq', 'Cuanto cuesta el corte de hombre', '100 pesos', 'precio,corte', 1, "
        "'2026-01-15 10:30:00', '2026-01-15 10:30:00', 1)"
    )
    conn.execute(
        "INSERT INTO business_knowledge (id, business_id, type, question, answer, tags, "
        "active, created_at, updated_at, created_by_user_id) VALUES "
        "(2, 1, 'instruction', 'Como agendar una cita', 'Usa la web o app', 'agendar,cita', 1, "
        "'2026-02-01 12:00:00', '2026-02-01 12:00:00', 1)"
    )
    conn.execute(
        "INSERT INTO business_knowledge (id, business_id, type, question, answer, tags, "
        "active, created_at, updated_at, created_by_user_id) VALUES "
        "(3, 2, 'faq', 'Precio del corte de mujer', '150 pesos', 'precio,corte', 1, "
        "'2026-03-20 08:00:00', '2026-03-20 08:00:00', 3)"
    )
    conn.execute(
        "INSERT INTO business_knowledge (id, business_id, type, question, answer, tags, "
        "active, created_at, updated_at, created_by_user_id) VALUES "
        "(4, 1, 'policy', 'Politica de cancelaciones', 'Cancela con 24h de anticipacion', "
        "'cancelacion,politica', 0, '2026-04-01 09:00:00', '2026-04-01 09:00:00', 1)"
    )

    # loyalty_settings (id=1)
    conn.execute(
        "INSERT INTO loyalty_settings (id, business_id, enabled, "
        "points_per_completed_appointment, created_at, updated_at) VALUES "
        "(1, 1, 1, 1, '2026-01-20 10:00:00', '2026-01-20 10:00:00')"
    )

    # loyalty_accounts (id=1)
    conn.execute(
        "INSERT INTO loyalty_accounts (id, business_id, customer_phone, customer_email, "
        "customer_name, points_balance, created_at, updated_at) VALUES "
        "(1, 1, '5551234', 'ana@test.com', 'Ana', 50, '2026-06-01 09:30:00', '2026-06-01 09:30:00')"
    )

    # rewards (id=1)
    conn.execute(
        "INSERT INTO rewards (id, business_id, name, description, points_cost, active, "
        "created_at, updated_at) VALUES "
        "(1, 1, 'Corte gratis', 'Un corte gratis al canjerar', 100, 1, "
        "'2026-02-10 11:00:00', '2026-02-10 11:00:00')"
    )

    # retention_actions (id=1)
    conn.execute(
        "INSERT INTO retention_actions (id, business_id, customer_phone, action_type, notes, "
        "actor_user_id, last_completed_date, created_at) VALUES "
        "(1, 1, '5551234', 'viewed', 'Vista oferta', 1, '2026-05-15', '2026-05-15 09:00:00')"
    )

    conn.commit()
    conn.close()

    return db_path, temp_dir


def test_integration_end_to_end(pg_enabled):
    """M. Integración end-to-end: migración completa + verificación exhaustiva."""
    from database.pg_pool import close_pg_pool, init_pg_pool
    from services.knowledge import search_knowledge_scoped
    from tests_pg._helpers import make_pg_proxy

    db_path, temp_dir = _create_representative_sqlite_db()

    dbname, db_conninfo, db_url = _setup_pg_target(pg_enabled)

    try:
        pool = init_pg_pool(None, conninfo=db_url)

        try:
            report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

            assert report.success, f"Migración falló: {report.errors}"

            with connect_autocommit(db_conninfo) as conn:
                # --- A. Conteo de filas por tabla ---
                counts = {
                    "businesses": 2,
                    "users": 3,
                    "business_settings": 2,
                    "weekly_schedules": 4,
                    "services": 3,
                    "resources": 1,
                    "appointments": 3,
                    "business_users": 2,
                    "business_knowledge": 4,
                    "loyalty_settings": 1,
                    "loyalty_accounts": 1,
                    "rewards": 1,
                    "retention_actions": 1,
                }
                for table, expected in counts.items():
                    actual = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    assert actual == expected, (
                        f"Tabla {table}: esperaba {expected} filas, obtuve {actual}"
                    )

                # --- B. IDs preservados ---
                for table, id_val in [
                    ("businesses", 1),
                    ("businesses", 2),
                    ("users", 1),
                    ("users", 3),
                    ("appointments", 1),
                    ("appointments", 2),
                    ("appointments", 3),
                    ("business_knowledge", 1),
                    ("business_knowledge", 4),
                    ("services", 2),
                    ("business_users", 2),
                ]:
                    exists = conn.execute(
                        f'SELECT 1 FROM "{table}" WHERE id = %s', (id_val,)
                    ).fetchone()
                    assert exists is not None, f"ID preservado falló: {table}.id={id_val}"

                # --- C. Foreign keys válidas ---
                bu = conn.execute(
                    "SELECT user_id, business_id, role_id FROM business_users WHERE id = 1"
                ).fetchone()
                assert bu[0] == 1 and bu[1] == 1 and bu[2] == 1

                appt_with_resource = conn.execute(
                    "SELECT resource_id FROM appointments WHERE id = 2"
                ).fetchone()
                assert appt_with_resource[0] == 1

                # --- D. BOOLEAN correctos ---
                svc3 = conn.execute("SELECT active FROM services WHERE id = 3").fetchone()[0]
                assert svc3 is False, "services.id=3.active debe ser False"

                biz_active = conn.execute(
                    "SELECT active, pending FROM businesses WHERE id = 1"
                ).fetchone()
                assert biz_active[0] is True and biz_active[1] is False

                kb4 = conn.execute("SELECT active FROM business_knowledge WHERE id = 4").fetchone()[
                    0
                ]
                assert kb4 is False, "business_knowledge.id=4.active debe ser False"

                # --- E. DATE/TIME/TIMESTAMP correctos ---
                appt1 = conn.execute(
                    "SELECT appointment_date, appointment_time, appointment_end, created_at "
                    "FROM appointments WHERE id = 1"
                ).fetchone()
                assert appt1[0] == datetime.date(2026, 6, 15)
                assert appt1[1] == datetime.time(10, 0)
                assert appt1[2] == datetime.time(10, 30)
                assert appt1[3] == datetime.datetime(2026, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)

                # --- F. NULL preservados ---
                appt2 = conn.execute(
                    "SELECT phone, customer_email, resource_id, idempotency_key "
                    "FROM appointments WHERE id = 2"
                ).fetchone()
                assert appt2[0] is None
                assert appt2[1] is None
                assert appt2[2] == 1  # resource_id = 1
                assert appt2[3] is None

                # --- G. Sequences ajustadas (INSERT nuevo obtiene ID correcto) ---
                new_biz_id = conn.execute(
                    "INSERT INTO businesses (name, slug, active, pending) "
                    "VALUES ('New Biz', 'new-biz', TRUE, FALSE) RETURNING id"
                ).fetchone()[0]
                assert new_biz_id == 3, f"Sequence businesses: esperaba 3, obtengo {new_biz_id}"

                new_appt_id = conn.execute(
                    "INSERT INTO appointments (customer_name, service, appointment_date, "
                    "appointment_time, appointment_end, duration, business_id) "
                    "VALUES ('Test', 'Corte', '2026-07-01', '10:00', '10:30', 30, 1) RETURNING id"
                ).fetchone()[0]
                assert new_appt_id == 4, f"Sequence appointments: esperaba 4, obtengo {new_appt_id}"

                # --- H. Columnas GENERATED calculadas ---
                tsearch_val = conn.execute(
                    "SELECT document_tsearch IS NOT NULL FROM business_knowledge WHERE id = 1"
                ).fetchone()[0]
                assert tsearch_val is True, "document_tsearch debe estar calculado (GENERATED)"

                gen_col = conn.execute(
                    "SELECT is_generated FROM information_schema.columns "
                    "WHERE table_name='business_knowledge' AND column_name='document_tsearch'"
                ).fetchone()
                assert gen_col is not None and gen_col[0] == "ALWAYS"

                # --- I. FTS funciona sobre datos migrados ---
                with patch("services.knowledge.get_connection", lambda: make_pg_proxy(pool)):
                    results = search_knowledge_scoped(1, "corte", limit=5)
                    assert len(results) >= 1, (
                        f"FTS debe encontrar al menos 1 resultado en tenant 1, obtengo {len(results)}"
                    )

                    results2 = search_knowledge_scoped(1, "precio", limit=5)
                    assert len(results2) >= 1

                    results3 = search_knowledge_scoped(2, "corte", limit=5)
                    found_mujer = any("mujer" in r["question"].lower() for r in results3)
                    assert found_mujer, (
                        "FTS multi-tenant: debe encontrar 'corte de mujer' en tenant 2"
                    )
        finally:
            close_pg_pool(pool=pool)

    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


def test_dry_run_on_representative_dataset(pg_enabled):
    """M. DRY-RUN sobre dataset representativo: no modifica PG."""
    db_path, temp_dir = _create_representative_sqlite_db()

    dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

    try:
        report = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=True)

        assert report.dry_run
        assert report.success
        assert report.total_rows_read > 0
        assert report.total_rows_inserted == 0

        with connect_autocommit(db_conninfo) as conn:
            biz_count = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
            assert biz_count == 0, "DRY-RUN no debe insertar datos"

            bk_count = conn.execute("SELECT COUNT(*) FROM business_knowledge").fetchone()[0]
            assert bk_count == 0, "DRY-RUN no debe insertar datos"
    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)


def test_rejects_non_empty_target_after_migration(pg_enabled):
    """M. Segunda migración contra BD ya poblada: rechaza con MigrationTargetNotEmptyError."""
    db_path, temp_dir = _create_representative_sqlite_db()

    dbname, db_conninfo, _ = _setup_pg_target(pg_enabled)

    try:
        report1 = migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)
        assert report1.success

        with pytest.raises(MigrationTargetNotEmptyError):
            migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=False)

        with pytest.raises(MigrationTargetNotEmptyError):
            migrate_sqlite_to_postgres(str(db_path), db_conninfo, dry_run=True)
    finally:
        temp_dir.cleanup()
        drop_test_database(pg_enabled, dbname)
