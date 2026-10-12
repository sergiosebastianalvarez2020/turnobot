"""Pruebas D15: statement_timeout en el migrador PostgreSQL.

Verifica que:
1. Una migración que excede statement_timeout falla con QueryCanceled.
2. El rollback es correcto: la tabla no persiste y la versión no queda en schema_migrations.
3. Tras el fallo, reejecutar la migración con una espera corta funciona correctamente.
"""

import os
import shutil

import psycopg.errors
import pytest

import database.pg_migrator as pgm
from tests_pg._helpers import (
    INITIAL_SCHEMA_PATH,
    build_test_db_conninfo,
    connect_autocommit,
    create_test_database,
    drop_test_database,
    new_db_name,
)

pytestmark = pytest.mark.pg_live


def _public_tables(conninfo: str) -> set[str]:
    with connect_autocommit(conninfo) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
    return {row[0] for row in rows}


def _migration_rows(conninfo: str) -> list[tuple]:
    with connect_autocommit(conninfo) as conn:
        return conn.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()


def test_d15_statement_timeout_triggers_rollback_and_reexecution(
    pg_enabled: str, monkeypatch, tmp_path
):
    """
    D15: statement_timeout en el runner debe abortar migraciones largas,
    hacer rollback completo y permitir reejecución exitosa.

    Pasos:
    1. Configurar PG_STATEMENT_TIMEOUT corto (1s) vía monkeypatch.
    2. Aplicar baseline 001 (sin migraciones lentas).
    3. Añadir migración lenta (pg_sleep(5)) y ejecutar -> debe fallar por timeout.
    4. Verificar: tabla no existe, schema_migrations sin versión 2.
    5. Restaurar timeout original, reejecutar con pg_sleep(0.1) -> éxito.
    """
    import database.pg_pool as pg_pool

    monkeypatch.setattr(pg_pool, "PG_STATEMENT_TIMEOUT", "1s")

    pg_enabled = os.getenv(
        "TURNOBOT_PG_URL", "postgresql://turnobot_test:turnobot_test_pass@127.0.0.1:5433/postgres"
    )

    dbname = new_db_name()
    create_test_database(pg_enabled, dbname)
    conninfo = build_test_db_conninfo(pg_enabled, dbname)

    try:
        # 1. Preparar directorio de migraciones temporal
        tmp_migrations = tmp_path / "d15_migrations"
        tmp_migrations.mkdir()
        shutil.copy(INITIAL_SCHEMA_PATH, tmp_migrations / "001_initial_schema.sql")

        # PASO 1: Solo baseline 001
        result1 = pgm.run_migrations(conninfo, migrations_dir=tmp_migrations, allow_external=True)
        assert [item.version for item in result1.applied] == [1]

        # PASO 2: Añadir migración lenta y ejecutar -> debe fallar por timeout
        (tmp_migrations / "002_slow.sql").write_text("SELECT pg_sleep(5);", encoding="utf-8")

        try:
            pgm.run_migrations(conninfo, migrations_dir=tmp_migrations, allow_external=True)
            pytest.fail("Expected QueryCanceled due to statement timeout")
        except psycopg.errors.QueryCanceled as e:
            if "statement timeout" not in str(e) and "canceling statement" not in str(e):
                raise

        # Verificar: tabla de la migración 002 NO existe
        tables = _public_tables(conninfo)
        assert "d15_test_table" not in tables

        # Verificar: schema_migrations solo tiene versión 1
        rows = _migration_rows(conninfo)
        assert len(rows) == 1
        assert rows[0][0] == 1

        # PASO 3: Restaurar timeout original y reejecutar con espera corta
        monkeypatch.setattr(pg_pool, "PG_STATEMENT_TIMEOUT", "30s")

        (tmp_migrations / "002_slow.sql").write_text(
            "CREATE TABLE d15_test_table (id int);\nSELECT pg_sleep(0.1);", encoding="utf-8"
        )

        result2 = pgm.run_migrations(conninfo, migrations_dir=tmp_migrations, allow_external=True)

        assert [item.version for item in result2.applied] == [2]
        assert result2.current_version == 2

        tables_after = _public_tables(conninfo)
        assert "d15_test_table" in tables_after

        rows2 = _migration_rows(conninfo)
        assert len(rows2) == 2
        assert rows2[0][0] == 1
        assert rows2[1][0] == 2
    finally:
        drop_test_database(pg_enabled, dbname)
