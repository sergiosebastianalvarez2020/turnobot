"""Pruebas LIVE del runner de migraciones PostgreSQL (migraciones incrementales).

Usan el PostgreSQL real (``TURNOBOT_PG_URL``) sobre bases descartables
``turnobot_test_<hex>``. NO tocan producción ni la suite SQLite: validan el
mecanismo de ``database/pg_migrator.py`` (baseline, adoption, incremental,
checksum, huecos, concurrencia, timeout del advisory lock y rollback).
"""

import hashlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import database.pg_migrator as pgm
from tests_pg._helpers import (
    INITIAL_SCHEMA_PATH,
    apply_initial_schema,
    build_test_db_conninfo,
    connect_autocommit,
    conninfo_to_url,
    create_test_database,
    drop_test_database,
    new_db_name,
)

pytestmark = pytest.mark.pg_live


@contextmanager
def _disposable_db(pg_enabled: str):
    dbname = new_db_name()
    create_test_database(pg_enabled, dbname)
    try:
        yield build_test_db_conninfo(pg_enabled, dbname)
    finally:
        drop_test_database(pg_enabled, dbname)


def _baseline_copy(tmp_path) -> str:
    shutil.copy(INITIAL_SCHEMA_PATH, tmp_path / "001_initial_schema.sql")
    return (tmp_path / "001_initial_schema.sql").read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def test_applies_baseline_on_empty_database_and_is_idempotent(pg_enabled):
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(conninfo)

        assert [item.version for item in result.applied] == [1]
        assert result.current_version == 1
        assert "businesses" in _public_tables(conninfo)
        assert "roles" in _public_tables(conninfo)

        rows = _migration_rows(conninfo)
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "001_initial_schema.sql"
        assert rows[0][2] == _sha256(INITIAL_SCHEMA_PATH.read_text(encoding="utf-8"))

        second = pgm.run_migrations(conninfo)
        assert second.applied == []
        assert second.adopted == []
        assert second.current_version == 1


def test_adopts_existing_baseline_without_reexecution(pg_enabled):
    """Base ya bootstrapeada por la app (001 completo, sin metadata): auto-baseline."""
    with _disposable_db(pg_enabled) as conninfo:
        apply_initial_schema(conninfo)

        result = pgm.run_migrations(conninfo)

        assert [item.version for item in result.adopted] == [1]
        assert result.applied == []
        assert result.current_version == 1
        rows = _migration_rows(conninfo)
        assert len(rows) == 1
        assert rows[0][0] == 1


def test_applies_incremental_migrations_in_order(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY, version TEXT NOT NULL);\n",
        encoding="utf-8",
    )
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        assert [item.version for item in result.applied] == [1, 2]
        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]
        assert "mig_widgets" in _public_tables(conninfo)


def test_incremental_after_existing_baseline(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        apply_initial_schema(conninfo)

        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        assert [item.version for item in result.adopted] == [1]
        assert [item.version for item in result.applied] == [2]
        assert "mig_widgets" in _public_tables(conninfo)


def test_checksum_mismatch_fails_before_applying(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        (tmp_path / "002_widgets.sql").write_text(
            "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n-- touched\n", encoding="utf-8"
        )
        with pytest.raises(pgm.MigrationError, match="Checksum"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]


def test_version_gap_fails_without_schema_changes(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "003_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        with pytest.raises(pgm.MigrationError, match="Huecos"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        tables = _public_tables(conninfo)
        assert "businesses" not in tables
        assert "mig_widgets" not in tables


def test_partial_schema_without_metadata_fails(pg_enabled):
    with _disposable_db(pg_enabled) as conninfo:
        with connect_autocommit(conninfo) as conn:
            conn.execute("CREATE TABLE businesses (id bigint PRIMARY KEY)")

        with pytest.raises(pgm.MigrationError, match="parcial"):
            pgm.run_migrations(conninfo)


def test_failed_migration_rolls_back_and_is_not_recorded(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\nSELECT 1/0;\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        with pytest.raises(Exception):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        assert [_row[0] for _row in _migration_rows(conninfo)] == [1]
        assert "mig_widgets" not in _public_tables(conninfo)


def test_dry_run_writes_nothing(pg_enabled):
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(conninfo, dry_run=True)

        assert result.dry_run is True
        assert [item.version for item in result.pending] == [1]
        assert _public_tables(conninfo) == set()


def test_concurrent_runners_serialize(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "SELECT pg_sleep(1);\nCREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        barrier = threading.Barrier(2)

        def run() -> pgm.MigrationResult:
            barrier.wait()
            return pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _: run(), (None, None))

        applied_total = len(first.applied) + len(second.applied)
        assert applied_total == 2
        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]
        assert "mig_widgets" in _public_tables(conninfo)


def test_lock_wait_times_out_when_lock_is_held_then_recovers(pg_enabled):
    with _disposable_db(pg_enabled) as conninfo:
        blocker = connect_autocommit(conninfo)
        try:
            blocker.execute("SELECT pg_advisory_lock(%s)", (pgm._PG_SCHEMA_LOCK,))

            with pytest.raises(pgm.MigrationLockTimeout, match="advisory lock"):
                pgm.run_migrations(conninfo, lock_timeout=0.5)

            assert _public_tables(conninfo) == set()
        finally:
            blocker.execute("SELECT pg_advisory_unlock(%s)", (pgm._PG_SCHEMA_LOCK,))
            blocker.close()

        result = pgm.run_migrations(conninfo)
        assert [item.version for item in result.applied] == [1]


def test_cli_dry_run_and_invalid_url(pg_enabled, capsys):
    from scripts import migrate_pg

    with _disposable_db(pg_enabled) as conninfo:
        code = migrate_pg.main(["--url", conninfo_to_url(conninfo), "--dry-run"])
        assert code == 0
        assert _public_tables(conninfo) == set()
        assert "Pendientes" in capsys.readouterr().out

    code = migrate_pg.main(["--url", "sqlite:///no.db"])
    assert code == 1
