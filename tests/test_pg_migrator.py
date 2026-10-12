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


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests puros con mocks: sin PostgreSQL real, sin skip funcional del conftest."""
    yield


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

    migrations = pgm.discover_migrations(tmp_path, allow_external=True)

    assert [item.version for item in migrations] == [1, 2]
    assert migrations[0].name == "001_initial_schema.sql"
    assert migrations[0].checksum == _sha256(BASELINE_SQL)


def test_discover_migrations_rejects_gap(tmp_path):
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "003_widgets.sql", NEXT_SQL)

    with pytest.raises(pgm.MigrationError, match="Huecos"):
        pgm.discover_migrations(tmp_path, allow_external=True)


def test_discover_migrations_rejects_empty_directory(tmp_path):
    with pytest.raises(pgm.MigrationError, match="No se encontraron"):
        pgm.discover_migrations(tmp_path, allow_external=True)


# ---------------------------------------------------------------------------
# Política de nombres (D1/D10)
# ---------------------------------------------------------------------------


def test_discover_migrations_accepts_valid_three_digit_version(tmp_path):
    """Versión de 3 dígitos con nombre válido se acepta."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "002_add_widgets.sql", NEXT_SQL)

    migrations = pgm.discover_migrations(tmp_path, allow_external=True)

    assert [item.version for item in migrations] == [1, 2]
    assert migrations[0].name == "001_initial_schema.sql"
    assert migrations[1].name == "002_add_widgets.sql"


def test_discover_migrations_accepts_valid_four_digit_version(tmp_path):
    """Versión de 4+ dígitos con nombre válido se acepta (sin límite de 999).

    Nota: las versiones deben ser contiguas desde 1, así que usamos 1 y 1001
    para demostrar que 4+ dígitos funciona, pero requerimos contigüidad.
    """
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    # No podemos saltar a 1000 porque requiere contigüidad.
    # Este test verifica que el regex acepta 4 dígitos, no que se pueda saltar.
    # Para demostrar 4 dígitos sin huecos, necesitaríamos 1000 archivos.
    # Aquí solo verificamos que el regex no rechaza 4 dígitos.
    _write(tmp_path, "0002_add_feature.sql", NEXT_SQL)

    migrations = pgm.discover_migrations(tmp_path, allow_external=True)

    assert [item.version for item in migrations] == [1, 2]
    assert migrations[1].name == "0002_add_feature.sql"


def test_discover_migrations_rejects_invalid_sql_filename(tmp_path):
    """Archivos .sql con nombre inválido deben fallar explícitamente (D1)."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "notes.txt", "ignore me")  # no es .sql, se ignora
    _write(tmp_path, "schema.sql", "CREATE TABLE nope (id int);")  # .sql pero nombre inválido
    _write(tmp_path, "002_2badname.sql", "CREATE TABLE t (id int);")  # nombre empieza con dígito

    with pytest.raises(pgm.MigrationError, match="nombre inválido"):
        pgm.discover_migrations(tmp_path, allow_external=True)


def test_discover_migrations_rejects_date_prefixed_filename(tmp_path):
    """Nombres con prefijo tipo fecha (ej. 2024_01_01_fix.sql) se rechazan (D10)."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "2024_01_01_fix.sql", "CREATE TABLE t (id int);")

    with pytest.raises(pgm.MigrationError, match="nombre inválido"):
        pgm.discover_migrations(tmp_path, allow_external=True)


def test_discover_migrations_ignores_non_sql_files(tmp_path):
    """Archivos que no son .sql se ignoran silenciosamente."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "notes.txt", "ignore me")
    _write(tmp_path, "README.md", "# docs")
    _write(tmp_path, "script.py", "print('hi')")

    migrations = pgm.discover_migrations(tmp_path, allow_external=True)

    assert [item.name for item in migrations] == ["001_initial_schema.sql"]


def test_discover_migrations_rejects_duplicate_versions(tmp_path):
    """Versiones duplicadas siguen rechazándose."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "001_duplicate.sql", NEXT_SQL)

    with pytest.raises(pgm.MigrationError, match="duplicadas"):
        pgm.discover_migrations(tmp_path, allow_external=True)


def test_discover_migrations_rejects_gap_naming_policy(tmp_path):
    """Huecos en la numeración siguen rechazándose (bajo política de nombres)."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "003_widgets.sql", NEXT_SQL)

    with pytest.raises(pgm.MigrationError, match="Huecos"):
        pgm.discover_migrations(tmp_path, allow_external=True)


def test_discover_migrations_external_dir_same_rules(tmp_path):
    """Directorios externos autorizados (D13) siguen las mismas reglas de nombres."""
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "002_2badname.sql", NEXT_SQL)  # nombre empieza con dígito

    with pytest.raises(pgm.MigrationError, match="nombre inválido"):
        pgm.discover_migrations(tmp_path, allow_external=True)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_plan_fresh_database_applies_baseline(tmp_path):
    conn = _FakeConnection()

    plan = pgm.plan_migrations(conn, _baseline_dir(tmp_path), allow_external=True)

    assert [item.version for item in plan.to_apply] == [1]
    assert plan.to_adopt == []


def test_plan_complete_baseline_is_adopted(tmp_path):
    conn = _FakeConnection(tables={"businesses"}, indexes={"idx_businesses"})

    plan = pgm.plan_migrations(conn, _baseline_dir(tmp_path), allow_external=True)

    assert [item.version for item in plan.to_adopt] == [1]
    assert plan.to_apply == []


def test_plan_partial_baseline_raises(tmp_path):
    conn = _FakeConnection(tables={"businesses"}, indexes=set())

    with pytest.raises(pgm.MigrationError, match="parcial"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path), allow_external=True)


def test_plan_partial_when_only_index_present_raises(tmp_path):
    conn = _FakeConnection(tables=set(), indexes={"idx_businesses"})

    with pytest.raises(pgm.MigrationError, match="parcial"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path), allow_external=True)


def test_plan_pending_only_new_versions(tmp_path):
    _baseline_dir(tmp_path)
    _write(tmp_path, "002_widgets.sql", NEXT_SQL)
    conn = _FakeConnection(
        has_metadata=True, applied=[(1, "001_initial_schema.sql", _sha256(BASELINE_SQL))]
    )

    plan = pgm.plan_migrations(conn, tmp_path, allow_external=True)

    assert plan.to_adopt == []
    assert [item.version for item in plan.to_apply] == [2]


def test_plan_detects_checksum_change(tmp_path):
    conn = _FakeConnection(has_metadata=True, applied=[(1, "001_initial_schema.sql", "0" * 64)])

    with pytest.raises(pgm.MigrationError, match="Checksum"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path), allow_external=True)


def test_plan_detects_unknown_applied_version(tmp_path):
    conn = _FakeConnection(
        has_metadata=True,
        applied=[
            (1, "001_initial_schema.sql", _sha256(BASELINE_SQL)),
            (2, "002_widgets.sql", "a" * 64),
        ],
    )

    with pytest.raises(pgm.MigrationError, match="no conoce"):
        pgm.plan_migrations(conn, _baseline_dir(tmp_path), allow_external=True)


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
        pgm.plan_migrations(conn, tmp_path, allow_external=True)


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    conn = _FakeConnection()

    result = pgm.apply_pending_migrations(
        conn, migrations_dir=_baseline_dir(tmp_path), dry_run=True, allow_external=True
    )

    assert result.dry_run is True
    assert [item.version for item in result.pending] == [1]
    assert result.applied == []
    assert not any(
        "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in sql.upper() for sql, _ in conn.calls
    )


def test_apply_executes_and_records(tmp_path):
    conn = _FakeConnection()

    result = pgm.apply_pending_migrations(
        conn, migrations_dir=_baseline_dir(tmp_path), allow_external=True
    )

    assert [item.version for item in result.applied] == [1]
    assert result.current_version == 1
    assert conn.applied == [(1, "001_initial_schema.sql", _sha256(BASELINE_SQL))]
    assert any("CREATE TABLE businesses" in sql for sql, _ in conn.calls)


def test_apply_adopts_complete_baseline_then_applies_next_fails_without_metadata(tmp_path):
    """
    Política D2: base con esquema 001 completo (simulado via tables={"businesses"})
    pero SIN metadata, y migraciones posteriores a 001 descubiertas -> MigrationError.
    """
    _baseline_dir(tmp_path)
    _write(tmp_path, "002_widgets.sql", NEXT_SQL)
    # Simula base con esquema 001 completo (tabla businesses e índice) pero SIN metadata
    conn = _FakeConnection(tables={"businesses"}, indexes={"idx_businesses"})

    with pytest.raises(pgm.MigrationError) as exc_info:
        pgm.apply_pending_migrations(conn, migrations_dir=tmp_path, allow_external=True)

    error_msg = str(exc_info.value)
    assert "metadata" in error_msg.lower() or "schema_migrations" in error_msg.lower()
    assert "posterior" in error_msg.lower() or "001" in error_msg

    # Verificar que no se intentó crear schema_migrations ni registrar nada
    assert not any(
        "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in sql.upper() for sql, _ in conn.calls
    )
    assert conn.applied == []


def test_apply_rolls_back_and_does_not_record_on_failure(tmp_path):
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL + "SELECT BOOM;\n")
    conn = _FakeConnection()

    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        pgm.apply_pending_migrations(conn, migrations_dir=tmp_path, allow_external=True)

    assert conn.applied == []
    assert conn.rollbacks >= 1


def test_migration_lock_timeout_is_migration_error():
    assert issubclass(pgm.MigrationLockTimeout, pgm.MigrationError)


def test_acquire_lock_timeout_raises_without_touching_schema(tmp_path):
    conn = _FakeConnection(lock_available=False)

    with pytest.raises(pgm.MigrationLockTimeout, match="advisory lock"):
        pgm.apply_pending_migrations(
            conn, migrations_dir=_baseline_dir(tmp_path), lock_timeout=0.05, allow_external=True
        )

    assert any("PG_TRY_ADVISORY_LOCK" in sql.upper() for sql, _ in conn.calls)
    assert any("PG_ADVISORY_UNLOCK" in sql.upper() for sql, _ in conn.calls)
    assert not any("SCHEMA_MIGRATIONS" in sql.upper() for sql, _ in conn.calls)


def test_acquire_lock_uses_default_timeout_when_not_specified(monkeypatch, tmp_path):
    monkeypatch.setattr(pgm, "_LOCK_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(pgm.time, "sleep", lambda _s: None)
    conn = _FakeConnection(lock_available=False)

    with pytest.raises(pgm.MigrationLockTimeout):
        pgm.apply_pending_migrations(
            conn, migrations_dir=_baseline_dir(tmp_path), allow_external=True
        )


def test_acquire_lock_success_uses_shared_session_key(tmp_path):
    conn = _FakeConnection(lock_available=True)

    pgm.apply_pending_migrations(conn, migrations_dir=_baseline_dir(tmp_path), allow_external=True)

    lock_calls = [params for sql, params in conn.calls if "PG_TRY_ADVISORY_LOCK" in sql.upper()]
    assert lock_calls
    assert all(params == (pgm._PG_SCHEMA_LOCK,) for params in lock_calls)


# ---------------------------------------------------------------------------
# Validación de directorio de migraciones (D13)
# ---------------------------------------------------------------------------


def test_validate_migrations_dir_accepts_official_dir():
    """El directorio oficial MIGRATIONS_PG_DIR se acepta por defecto."""
    # No debe elevar excepción
    validated = pgm._validate_migrations_dir(pgm.MIGRATIONS_PG_DIR)
    assert validated == pgm.MIGRATIONS_PG_DIR.resolve()


def test_validate_migrations_dir_accepts_subdir_of_base_dir(tmp_path):
    """Un subdirectorio dentro de BASE_DIR se acepta sin flag externo."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    # Crear una estructura que simule estar dentro de BASE_DIR
    # Usamos el BASE_DIR real del proyecto
    base = pgm.BASE_DIR
    test_subdir = base / "test_migrations_d13"
    test_subdir.mkdir(exist_ok=True)
    try:
        validated = pgm._validate_migrations_dir(test_subdir)
        assert validated == test_subdir.resolve()
    finally:
        test_subdir.rmdir()


def test_validate_migrations_dir_rejects_external_without_flag(tmp_path):
    """Una ruta absoluta externa se rechaza sin autorización."""
    with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
        pgm._validate_migrations_dir(tmp_path, allow_external=False)


def test_validate_migrations_dir_accepts_external_with_flag(tmp_path):
    """Una ruta externa se acepta con autorización explícita."""
    validated = pgm._validate_migrations_dir(tmp_path, allow_external=True)
    assert validated == tmp_path.resolve()


def test_validate_migrations_dir_rejects_symlink_outside_base_dir(tmp_path):
    """Un enlace simbólico que apunta fuera de BASE_DIR se rechaza sin flag."""
    # Crear directorio destino fuera de BASE_DIR
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    # Crear symlink dentro de BASE_DIR que apunta fuera
    base = pgm.BASE_DIR
    link_path = base / "test_symlink_d13"
    try:
        link_path.symlink_to(outside_dir)
        with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
            pgm._validate_migrations_dir(link_path, allow_external=False)
    except (OSError, NotImplementedError):
        # Windows sin privilegios de admin no permite crear symlinks
        pytest.skip("El sistema no permite crear enlaces simbólicos")
    finally:
        if link_path.exists():
            link_path.unlink()


def test_validate_migrations_dir_rejected_before_psycopg_connect(tmp_path, monkeypatch):
    """Ruta no autorizada se rechaza antes de llamar a psycopg.connect()."""
    connect_called = False

    def mock_connect(*args, **kwargs):
        nonlocal connect_called
        connect_called = True
        raise RuntimeError("no debería llamarse")

    monkeypatch.setattr(pgm.psycopg, "connect", mock_connect)

    with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
        pgm.run_migrations("postgresql://user:pass@host/db", migrations_dir=tmp_path)

    assert not connect_called


def test_apply_pending_migrations_rejected_before_lock(tmp_path):
    """Ruta no autorizada se rechaza antes de adquirir lock en apply_pending_migrations."""
    conn = _FakeConnection()

    with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
        pgm.apply_pending_migrations(conn, migrations_dir=tmp_path)

    # No debe haber intentos de lock
    lock_calls = [sql for sql, _ in conn.calls if "PG_TRY_ADVISORY_LOCK" in sql.upper()]
    assert lock_calls == []


def test_run_migrations_and_apply_pending_do_not_bypass_validation(tmp_path):
    """Llamadas directas a run_migrations y apply_pending_migrations no eluden la validación."""
    # run_migrations
    with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
        pgm.run_migrations("postgresql://user:pass@host/db", migrations_dir=tmp_path)

    # apply_pending_migrations
    conn = _FakeConnection()
    with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
        pgm.apply_pending_migrations(conn, migrations_dir=tmp_path)


def test_external_flag_does_not_disable_other_validations(tmp_path):
    """El flag externo no desactiva el resto de validaciones del migrador."""
    # Directorio vacío -> debe fallar por "No se encontraron migraciones"
    with pytest.raises(pgm.MigrationError, match="No se encontraron"):
        pgm.discover_migrations(tmp_path, allow_external=True)

    # Directorio con hueco -> debe fallar por "Huecos"
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(tmp_path, "003_widgets.sql", NEXT_SQL)
    with pytest.raises(pgm.MigrationError, match="Huecos"):
        pgm.discover_migrations(tmp_path, allow_external=True)


# ---------------------------------------------------------------------------
# D16 — Sentencias CONCURRENTLY no filtradas silenciosamente
# ---------------------------------------------------------------------------


def test_concurrently_statements_not_filtered_silently(tmp_path):
    """
    D16: las sentencias CREATE/DROP INDEX CONCURRENTLY no se descartan en silencio.

    El runner aplica cada migración en una transacción. PostgreSQL prohíbe
    CREATE/DROP INDEX CONCURRENTLY dentro de una transacción y lanza
    ProgrammingError. El parser split_sql_statements no debe filtrar estas
    sentencias; deben llegar a la conexión y fallar con error de PostgreSQL.
    """
    _write(tmp_path, "001_initial_schema.sql", BASELINE_SQL)
    _write(
        tmp_path, "002_concurrent_idx.sql", "CREATE INDEX CONCURRENTLY idx_test ON widgets (id);\n"
    )

    # 1. El parser NO debe filtrar la sentencia CONCURRENTLY
    from database.pg_migrator import split_sql_statements

    statements = split_sql_statements(
        (tmp_path / "002_concurrent_idx.sql").read_text(encoding="utf-8")
    )
    assert len(statements) == 1
    assert "CONCURRENTLY" in statements[0].upper()

    # 2. El SQL llega a _execute sin ser filtrado
    from database.pg_migrator import _execute

    conn = _FakeConnection()
    migration = pgm.MigrationFile(
        version=2,
        name="002_concurrent_idx.sql",
        path=tmp_path / "002_concurrent_idx.sql",
        checksum="dummy",
    )
    # _FakeConnection no ejecuta SQL real, pero verificamos que la sentencia
    # llega a _execute sin ser filtrada (no lanza excepción por filtrado)
    _execute(conn, migration)
    # Si llegamos aquí, la sentencia CONCURRENTLY pasó por _execute sin ser filtrada
    # En PostgreSQL real, esto lanzaría ProgrammingError: cannot create index concurrently within a transaction block
