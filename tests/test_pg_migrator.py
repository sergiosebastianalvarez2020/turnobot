"""Tests unitarios del runner de migraciones PostgreSQL (sin PostgreSQL real).

Cubren el descubrimiento/validación de archivos y el cálculo del plan con una
conexión simulada: base vacía, baseline adoptado, esquema parcial, checksum
modificado, versión desconocida, registro no contiguo, dry-run, rollback y el
timeout del advisory lock (``MigrationLockTimeout``).
"""

import hashlib
from pathlib import Path

import pytest

import database.pg_migrator as pgm

BASELINE_SQL = (
    "CREATE TABLE businesses (id bigint);\nCREATE INDEX idx_businesses ON businesses (id);\n"
)
NEXT_SQL = "CREATE TABLE widgets (id bigint);\n"


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    """Conexión DB-API simulada mínima para ejercitar el plan y la ejecución."""

    def __init__(
        self, *, tables=(), indexes=(), applied=(), has_metadata=False, lock_available=True
    ):
        self.tables = set(tables)
        self.indexes = set(indexes)
        self.applied = [tuple(row) for row in applied]
        self.has_metadata = has_metadata
        self.lock_available = lock_available
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        upper = sql.upper()
        if "PG_TRY_ADVISORY_LOCK" in upper:
            return _Result([(self.lock_available,)])
        if "TO_REGCLASS" in upper:
            return _Result([("schema_migrations" if self.has_metadata else None,)])
        if "SELECT VERSION, NAME, CHECKSUM FROM SCHEMA_MIGRATIONS" in upper:
            return _Result(self.applied)
        if "COALESCE(MAX(VERSION)" in upper:
            return _Result([(max((row[0] for row in self.applied), default=0),)])
        if "INFORMATION_SCHEMA.TABLES" in upper:
            return _Result([(table,) for table in self.tables])
        if "FROM PG_INDEXES" in upper:
            return _Result([(index,) for index in self.indexes])
        if "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in upper:
            self.has_metadata = True
            return _Result()
        if upper.startswith("INSERT INTO SCHEMA_MIGRATIONS"):
            version, name, checksum = params
            self.applied.append((version, name, checksum))
            return _Result()
        if "BOOM" in upper:
            raise RuntimeError("simulated DDL failure")
        return _Result()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def _sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _baseline_dir(tmp_path: Path) -> Path:
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    return tmp_path


# ---------------------------------------------------------------------------
# Descubrimiento de archivos
# ---------------------------------------------------------------------------


def test_discover_migrations_orders_by_version_and_hashes(tmp_path):
    _write(tmp_path, "002_widgets.sql", NEXT_SQL)
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)

    migrations = pgm.discover_migrations(tmp_path)

    assert [item.version for item in migrations] == [1, 2]
    assert migrations[0].name == "001_initial_schema.sql"
    assert migrations[0].checksum == _sha256(BASELINE_SQL)


def test_discover_migrations_rejects_gap(tmp_path):
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "003_widgets.sql", NEXT_SQL)

    with pytest.raises(pgm.MigrationError, match="Huecos"):
        pgm.discover_migrations(tmp_path)


def test_discover_migrations_rejects_empty_directory(tmp_path):
    with pytest.raises(pgm.MigrationError, match="No se encontraron"):
        pgm.discover_migrations(tmp_path)


def test_discover_migrations_ignores_non_matching_files(tmp_path):
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "notes.txt", "ignore me")
    _write(tmp_path, "schema.sql", "CREATE TABLE nope (id int);")

    migrations = pgm.discover_migrations(tmp_path)

    assert [item.name for item in migrations] == ["001_initial_schema.sql"]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_plan_fresh_database_applies_baseline(tmp_path):
    conn = _FakeConnection()

    plan = pgm.plan_migrations(conn, _baseline_dir(tmp_path))

    assert [item.version for item in plan.to_apply] == [1]
    assert plan.to_adopt == []


def test_plan_complete_baseline_is_adopted(tmp_path):
    conn = _FakeConnection(tables={"businesses"}, indexes={"idx_businesses"})

    plan = pgm.plan_migrations(conn, _baseline_dir(tmp_path))

    assert [item.version for item in plan.to_adopt] == [1]
    assert plan.to_apply == []


def test_plan_partial_baseline_raises(tmp_path):
    conn = _FakeConnection(tables={"businesses"}, indexes=set())

    with pytest.raises(pgm.MigrationError, match="parcial"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path))


def test_plan_partial_when_only_index_present_raises(tmp_path):
    conn = _FakeConnection(tables=set(), indexes={"idx_businesses"})

    with pytest.raises(pgm.MigrationError, match="parcial"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path))


def test_plan_pending_only_new_versions(tmp_path):
    _baseline_dir(tmp_path)
    _write(tmp_path, "002_widgets.sql", NEXT_SQL)
    conn = _FakeConnection(
        has_metadata=True, applied=[(1, "001_initial_schema.sql", _sha256(BASELINE_SQL))]
    )

    plan = pgm.plan_migrations(conn, tmp_path)

    assert plan.to_adopt == []
    assert [item.version for item in plan.to_apply] == [2]


def test_plan_detects_checksum_change(tmp_path):
    conn = _FakeConnection(has_metadata=True, applied=[(1, "001_initial_schema.sql", "0" * 64)])

    with pytest.raises(pgm.MigrationError, match="Checksum"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path))


def test_plan_detects_unknown_applied_version(tmp_path):
    conn = _FakeConnection(
        has_metadata=True,
        applied=[
            (1, "001_initial_schema.sql", _sha256(BASELINE_SQL)),
            (2, "002_widgets.sql", "a" * 64),
        ],
    )

    with pytest.raises(pgm.MigrationError, match="no conoce"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path))


def test_plan_detects_non_contiguous_registry(tmp_path):
    _baseline_dir(tmp_path)
    _write(tmp_path, "002_widgets.sql", NEXT_SQL)
    _write(tmp_path, "003_more.sql", NEXT_SQL)
    conn = _FakeConnection(
        has_metadata=True,
        applied=[
            (1, "001_initial_schema.sql", _sha256(BASELINE_SQL)),
            (3, "003_more.sql", _sha256(NEXT_SQL)),
        ],
    )

    with pytest.raises(pgm.MigrationError, match="no contiguo"):
        pgm.plan_migrations(conn, tmp_path)


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    conn = _FakeConnection()

    result = pgm.apply_pending_migrations(
        conn, migrations_dir=_baseline_dir(tmp_path), dry_run=True
    )

    assert result.dry_run is True
    assert [item.version for item in result.pending] == [1]
    assert result.applied == []
    assert not any(
        "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in sql.upper() for sql, _ in conn.calls
    )


def test_apply_executes_and_records(tmp_path):
    conn = _FakeConnection()

    result = pgm.apply_pending_migrations(conn, migrations_dir=_baseline_dir(tmp_path))

    assert [item.version for item in result.applied] == [1]
    assert result.current_version == 1
    assert conn.applied == [(1, "001_initial_schema.sql", _sha256(BASELINE_SQL))]
    assert any("CREATE TABLE businesses" in sql for sql, _ in conn.calls)


def test_apply_adopts_complete_baseline_then_applies_next(tmp_path):
    _baseline_dir(tmp_path)
    _write(tmp_path, "002_widgets.sql", NEXT_SQL)
    conn = _FakeConnection(tables={"businesses"}, indexes={"idx_businesses"})

    result = pgm.apply_pending_migrations(conn, migrations_dir=tmp_path)

    assert [item.version for item in result.adopted] == [1]
    assert [item.version for item in result.applied] == [2]
    assert result.current_version == 2


def test_apply_rolls_back_and_does_not_record_on_failure(tmp_path):
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL + "SELECT BOOM;\n")
    conn = _FakeConnection()

    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        pgm.apply_pending_migrations(conn, migrations_dir=tmp_path)

    assert conn.applied == []
    assert conn.rollbacks >= 1


def test_migration_lock_timeout_is_migration_error():
    assert issubclass(pgm.MigrationLockTimeout, pgm.MigrationError)


def test_acquire_lock_timeout_raises_without_touching_schema(tmp_path):
    conn = _FakeConnection(lock_available=False)

    with pytest.raises(pgm.MigrationLockTimeout, match="advisory lock"):
        pgm.apply_pending_migrations(
            conn, migrations_dir=_baseline_dir(tmp_path), lock_timeout=0.05
        )

    assert any("PG_TRY_ADVISORY_LOCK" in sql.upper() for sql, _ in conn.calls)
    assert any("PG_ADVISORY_UNLOCK" in sql.upper() for sql, _ in conn.calls)
    assert not any("SCHEMA_MIGRATIONS" in sql.upper() for sql, _ in conn.calls)


def test_acquire_lock_uses_default_timeout_when_not_specified(monkeypatch, tmp_path):
    monkeypatch.setattr(pgm, "_LOCK_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(pgm.time, "sleep", lambda _s: None)
    conn = _FakeConnection(lock_available=False)

    with pytest.raises(pgm.MigrationLockTimeout):
        pgm.apply_pending_migrations(conn, migrations_dir=_baseline_dir(tmp_path))


def test_acquire_lock_success_uses_shared_session_key(tmp_path):
    conn = _FakeConnection(lock_available=True)

    pgm.apply_pending_migrations(conn, migrations_dir=_baseline_dir(tmp_path))

    lock_calls = [params for sql, params in conn.calls if "PG_TRY_ADVISORY_LOCK" in sql.upper()]
    assert lock_calls
    assert all(params == (pgm._PG_SCHEMA_LOCK,) for params in lock_calls)
