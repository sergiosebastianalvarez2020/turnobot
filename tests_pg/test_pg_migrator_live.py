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

import psycopg
import psycopg.pq
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


def test_applies_baseline_on_empty_database_and_is_idempotent(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert [item.version for item in result.applied] == [1]
        assert result.current_version == 1
        assert "businesses" in _public_tables(conninfo)
        assert "roles" in _public_tables(conninfo)

        rows = _migration_rows(conninfo)
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "001_initial_schema.sql"
        assert rows[0][2] == _sha256(INITIAL_SCHEMA_PATH.read_text(encoding="utf-8"))

        second = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)
        assert second.applied == []
        assert second.adopted == []
        assert second.current_version == 1


def test_adopts_existing_baseline_without_reexecution(pg_enabled, tmp_path):
    """Base ya bootstrapeada por la app (001 completo, sin metadata): auto-baseline."""
    _baseline_copy(tmp_path)
    with _disposable_db(pg_enabled) as conninfo:
        apply_initial_schema(conninfo)

        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

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
        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert [item.version for item in result.applied] == [1, 2]
        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]
        assert "mig_widgets" in _public_tables(conninfo)


def test_incremental_after_existing_baseline_fails_without_metadata(pg_enabled, tmp_path):
    """
    Política D2: si el bootstrap de la app (apply_initial_schema) crea el esquema 001
    pero NO la tabla schema_migrations, y existen migraciones posteriores a 001,
    el migrador DEBE fallar con MigrationError. No se permite auto-baseline
    silencioso que re-ejecutaría migraciones posteriores.
    """
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        apply_initial_schema(conninfo)  # Crea esquema 001, SIN metadata

        with pytest.raises(pgm.MigrationError) as exc_info:
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        error_msg = str(exc_info.value)
        assert "metadata" in error_msg.lower() or "schema_migrations" in error_msg.lower()
        assert "posterior" in error_msg.lower() or "001" in error_msg

        # Verificar que NO se creó schema_migrations ni se ejecutó 002
        tables = _public_tables(conninfo)
        assert "mig_widgets" not in tables
        with connect_autocommit(conninfo) as conn:
            exists = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
        assert exists is None


def test_checksum_mismatch_fails_before_applying(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        (tmp_path / "002_widgets.sql").write_text(
            "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n-- touched\n", encoding="utf-8"
        )
        with pytest.raises(pgm.MigrationError, match="Checksum"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]


def test_version_gap_fails_without_schema_changes(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    (tmp_path / "003_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        with pytest.raises(pgm.MigrationError, match="Huecos"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

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
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert [_row[0] for _row in _migration_rows(conninfo)] == [1]
        assert "mig_widgets" not in _public_tables(conninfo)


def test_dry_run_writes_nothing(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(
            conninfo, dry_run=True, migrations_dir=tmp_path, allow_external=True
        )

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
            return pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _: run(), (None, None))

        applied_total = len(first.applied) + len(second.applied)
        assert applied_total == 2
        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]
        assert "mig_widgets" in _public_tables(conninfo)


def test_lock_wait_times_out_when_lock_is_held_then_recovers(pg_enabled, tmp_path):
    _baseline_copy(tmp_path)
    with _disposable_db(pg_enabled) as conninfo:
        blocker = connect_autocommit(conninfo)
        try:
            blocker.execute("SELECT pg_advisory_lock(%s)", (pgm._PG_SCHEMA_LOCK,))

            with pytest.raises(pgm.MigrationLockTimeout, match="advisory lock"):
                pgm.run_migrations(
                    conninfo, lock_timeout=0.5, migrations_dir=tmp_path, allow_external=True
                )

            assert _public_tables(conninfo) == set()
        finally:
            blocker.execute("SELECT pg_advisory_unlock(%s)", (pgm._PG_SCHEMA_LOCK,))
            blocker.close()

        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)
        assert [item.version for item in result.applied] == [1]


def test_release_lock_after_aborted_transaction_releases_session_lock(pg_enabled):
    """D5: ``_release_lock`` con transacción abortada debe liberar el lock de sesión.

    Reproduce el defecto D5: si la transacción está abortada, el
    ``pg_advisory_unlock`` inicial no llega a ejecutarse y sólo se hace
    ``rollback``; el advisory lock de SESIÓN (``pg_try_advisory_lock``) queda
    retenido hasta cerrar la conexión. Con una conexión de pool eso bloquea a
    todos los runners posteriores (``MigrationLockTimeout`` a los 60 s).

    La verificación es objetiva: una SEGUNDA conexión intenta adquirir el mismo
    lock del migrador después de ``_release_lock``; sólo puede hacerlo si el
    lock se liberó de verdad.
    """
    with _disposable_db(pg_enabled) as conninfo:
        holder = psycopg.connect(conninfo)
        try:
            pgm._acquire_lock(holder)
            assert holder.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

            # Abortar la transacción sin rollback explícito.
            with pytest.raises(psycopg.errors.DivisionByZero):
                holder.execute("SELECT 1/0")
            assert holder.info.transaction_status == psycopg.pq.TransactionStatus.INERROR

            # No debe lanzar: la rama de excepción absorbe el error y hace rollback.
            pgm._release_lock(holder)

            # Verificación objetiva con una segunda conexión.
            probe = psycopg.connect(conninfo, autocommit=True)
            try:
                acquired = probe.execute(
                    "SELECT pg_try_advisory_lock(%s)", (pgm._PG_SCHEMA_LOCK,)
                ).fetchone()[0]
                if acquired:
                    probe.execute("SELECT pg_advisory_unlock(%s)", (pgm._PG_SCHEMA_LOCK,))
                assert acquired is True, (
                    "D5: _release_lock no liberó el advisory lock de sesión tras una "
                    "transacción abortada; el lock quedó retenido hasta cerrar la conexión."
                )
            finally:
                probe.close()
        finally:
            holder.close()


def test_cli_dry_run_and_invalid_url(pg_enabled, capsys):
    from scripts import migrate_pg

    with _disposable_db(pg_enabled) as conninfo:
        code = migrate_pg.main(["--url", conninfo_to_url(conninfo), "--dry-run"])
        assert code == 0
        assert _public_tables(conninfo) == set()
        assert "Pendientes" in capsys.readouterr().out

    code = migrate_pg.main(["--url", "sqlite:///no.db"])
    assert code == 1


def test_cli_lock_timeout_flag_limits_wait_on_held_lock(pg_enabled, capsys):
    """D7: ``--lock-timeout`` acota la espera real del advisory lock en la CLI."""
    from scripts import migrate_pg

    with _disposable_db(pg_enabled) as conninfo:
        blocker = connect_autocommit(conninfo)
        try:
            blocker.execute("SELECT pg_advisory_lock(%s)", (pgm._PG_SCHEMA_LOCK,))

            code = migrate_pg.main(["--url", conninfo_to_url(conninfo), "--lock-timeout", "0.5"])

            assert code == 1
            err = capsys.readouterr().err
            assert "ERROR" in err
            assert "advisory lock" in err
            assert _public_tables(conninfo) == set()
        finally:
            blocker.execute("SELECT pg_advisory_unlock(%s)", (pgm._PG_SCHEMA_LOCK,))
            blocker.close()

        code_ok = migrate_pg.main(
            ["--url", conninfo_to_url(conninfo), "--lock-timeout", "5", "--dry-run"]
        )
        assert code_ok == 0
        assert "Pendientes" in capsys.readouterr().out


def test_metadata_loss_detection_regresses_d2(pg_enabled, tmp_path):
    """
    Regresión D2: pérdida de metadata (schema_migrations) no debe causar
    re-ejecución silenciosa de migraciones posteriores.

    Escenario:
    1. Base fresca -> migraciones 001 y 002 se aplican.
    2. 002 crea tabla marcador y registra una fila.
    3. Se elimina EXCLUSIVAMENTE schema_migrations.
    3. Al volver a ejecutar el migrador, el runner DEBE detectar el estado
       inconsistente (esquema completo sin metadata) y fallar con MigrationError
       ANTES de re-ejecutar 002.

    El defecto actual: _classify_against_baseline ve 001 como "complete"
    y hace auto-baseline, luego intenta aplicar 002 otra vez.
    """
    # 1. Preparar directorio de migraciones temporal con 001 real y 002 sintética
    _baseline_copy(tmp_path)
    migration_002_sql = """
CREATE TABLE IF NOT EXISTS mig_d2_marker (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    migration_version INT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO mig_d2_marker (migration_version) VALUES (2);
"""
    (tmp_path / "002_d2_marker.sql").write_text(migration_002_sql, encoding="utf-8")

    with _disposable_db(pg_enabled) as conninfo:
        # 2. Primera ejecución: ambas migraciones se aplican
        result1 = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)
        assert [item.version for item in result1.applied] == [1, 2]
        assert result1.current_version == 2

        # Verificar que schema_migrations tiene ambas versiones
        rows1 = _migration_rows(conninfo)
        assert [row[0] for row in rows1] == [1, 2]

        # Verificar marcador: una sola fila para versión 2
        with connect_autocommit(conninfo) as conn:
            marker_count_1 = conn.execute(
                "SELECT COUNT(*) FROM mig_d2_marker WHERE migration_version = 2"
            ).fetchone()[0]
        assert marker_count_1 == 1

        # 3. Simular pérdida de metadata: DROP schema_migrations SOLAMENTE
        with connect_autocommit(conninfo) as conn:
            conn.execute("DROP TABLE schema_migrations")

        # Confirmar que la tabla ya no existe
        with connect_autocommit(conninfo) as conn:
            exists = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
        assert exists is None

        # 4. Segunda ejecución: el migrador DEBE detectar la pérdida y fallar
        # con MigrationError antes de re-ejecutar 002.
        with pytest.raises(pgm.MigrationError) as exc_info:
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        # El error debe ser explicativo sobre la pérdida de metadata / estado inconsistente
        error_msg = str(exc_info.value)
        assert (
            "metadata" in error_msg.lower()
            or "schema_migrations" in error_msg.lower()
            or "inconsistente" in error_msg.lower()
            or "parcial" in error_msg.lower()
        )

        # 5. Verificar que el marcador NO se duplicó (002 no se re-ejecutó)
        with connect_autocommit(conninfo) as conn:
            marker_count_2 = conn.execute(
                "SELECT COUNT(*) FROM mig_d2_marker WHERE migration_version = 2"
            ).fetchone()[0]
        assert marker_count_2 == 1, (
            "El marcador se duplicó: la migración 002 se re-ejecutó silenciosamente "
            "tras la pérdida de schema_migrations. Esto confirma el defecto D2."
        )


# ---------------------------------------------------------------------------
# Validación de directorio de migraciones (D13) - Tests live
# ---------------------------------------------------------------------------


def test_run_migrations_rejects_external_dir_without_flag(pg_enabled, tmp_path):
    """Sin autorización, un directorio externo se rechaza antes de modificar la base."""
    _baseline_copy(tmp_path)
    with _disposable_db(pg_enabled) as conninfo:
        with pytest.raises(pgm.MigrationError, match="fuera del repositorio"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path)

        # Verificar que la base no fue modificada
        assert _public_tables(conninfo) == set()
        with connect_autocommit(conninfo) as conn:
            exists = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
        assert exists is None


def test_run_migrations_accepts_external_dir_with_flag(pg_enabled, tmp_path):
    """Con autorización explícita, un directorio temporal válido puede ejecutarse normalmente."""
    _baseline_copy(tmp_path)
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert [item.version for item in result.applied] == [1]
        assert result.current_version == 1
        assert "businesses" in _public_tables(conninfo)
        rows = _migration_rows(conninfo)
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "001_initial_schema.sql"


def test_run_migrations_external_flag_preserves_checksum_validation(pg_enabled, tmp_path):
    """El flag externo no desactiva la validación de checksums."""
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        # Modificar el archivo para cambiar el checksum
        (tmp_path / "002_widgets.sql").write_text(
            "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n-- modified\n", encoding="utf-8"
        )

        with pytest.raises(pgm.MigrationError, match="Checksum"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        # Verificar que no se aplicó la migración modificada
        assert [_row[0] for _row in _migration_rows(conninfo)] == [1, 2]


def test_run_migrations_external_flag_preserves_gap_validation(pg_enabled, tmp_path):
    """El flag externo no desactiva la validación de huecos de versión."""
    _baseline_copy(tmp_path)
    (tmp_path / "003_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        with pytest.raises(pgm.MigrationError, match="Huecos"):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert _public_tables(conninfo) == set()


def test_run_migrations_external_flag_preserves_rollback_on_failure(pg_enabled, tmp_path):
    """El flag externo no desactiva el rollback en caso de fallo de DDL."""
    _baseline_copy(tmp_path)
    (tmp_path / "002_widgets.sql").write_text(
        "CREATE TABLE mig_widgets (id bigint PRIMARY KEY);\nSELECT 1/0;\n", encoding="utf-8"
    )
    with _disposable_db(pg_enabled) as conninfo:
        with pytest.raises(Exception):
            pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)

        assert [_row[0] for _row in _migration_rows(conninfo)] == [1]
        assert "mig_widgets" not in _public_tables(conninfo)


# ---------------------------------------------------------------------------
# D4 — Prueba dinámica del carácter % en migraciones
# ---------------------------------------------------------------------------


def test_d4_percent_literal_in_migrator_path(pg_enabled, tmp_path):
    """
    D4: Ruta del migrador (conexión psycopg directa) con '%' literal en migración.

    La migración 002 incluye:
    - DEFAULT con '%' literal:  'valor%defecto'
    - COMMENT con '%' literal:  'Tabla con 100% cobertura'
    - Índice parcial con LIKE '%pattern%'

    Objetivo: verificar si la ruta cruda (psycopg.connect directo) almacena
    correctamente el '%' simple o falla / duplica el porcentaje.
    """
    _baseline_copy(tmp_path)
    migration_002_sql = """
CREATE TABLE d4_test (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    txt TEXT DEFAULT 'valor%defecto'
);
COMMENT ON TABLE d4_test IS 'Tabla con 100% cobertura';
CREATE INDEX d4_test_txt_like_idx ON d4_test (txt) WHERE txt LIKE '%pattern%';
"""
    (tmp_path / "002_d4_percent.sql").write_text(migration_002_sql, encoding="utf-8")

    with _disposable_db(pg_enabled) as conninfo:
        # Ejecutar migración vía run_migrations (ruta cruda: psycopg.connect directo)
        result = pgm.run_migrations(conninfo, migrations_dir=tmp_path, allow_external=True)
        assert [item.version for item in result.applied] == [1, 2]
        assert result.current_version == 2

        # Verificar valores almacenados
        with connect_autocommit(conninfo) as conn:
            # 1. DEFAULT value
            default = conn.execute(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='d4_test' AND column_name='txt'"
            ).fetchone()[0]
            # PostgreSQL almacena el default como 'valor%defecto'::text
            assert default is not None
            assert "valor%defecto" in default, f"DEFAULT almacenado incorrecto: {default!r}"

            # 2. COMMENT
            comment = conn.execute("SELECT obj_description('d4_test'::regclass)").fetchone()[0]
            assert comment is not None
            assert "Tabla con 100% cobertura" in comment, (
                f"COMMENT almacenado incorrecto: {comment!r}"
            )

            # 3. Índice parcial con LIKE '%pattern%'
            # PostgreSQL almacena LIKE como operador ~~ en pg_indexes.indexdef
            idx_def = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'd4_test_txt_like_idx'"
            ).fetchone()[0]
            assert idx_def is not None
            # PostgreSQL normaliza LIKE a operador ~~ en la definición del índice
            assert "txt ~~ '%pattern%'::text" in idx_def, (
                f"Índice parcial almacenado incorrecto: {idx_def!r}"
            )


def test_d4_percent_literal_in_proxy_path(pg_enabled, tmp_path):
    """
    D4: Ruta adaptada (PgConnectionProxy -> adapt_query_for_postgres -> escape_literal_percent)
    con '%' literal en consulta SQL.

    Ejecuta las mismas sentencias DDL a través de PgConnectionProxy, que invoca
    adapt_query_for_postgres -> escape_literal_percent antes de ejecutar.

    Objetivo: comparar el resultado almacenado con la ruta del migrador.
    """
    from database.pg_pool import PgConnectionProxy, create_pg_pool, split_sql_statements

    # Configurar pool real sobre la base de prueba
    _baseline_copy(tmp_path)
    migration_002_sql = """
CREATE TABLE d4_test (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    txt TEXT DEFAULT 'valor%defecto'
);
COMMENT ON TABLE d4_test IS 'Tabla con 100% cobertura';
CREATE INDEX d4_test_txt_like_idx ON d4_test (txt) WHERE txt LIKE '%pattern%';
"""
    (tmp_path / "002_d4_percent.sql").write_text(migration_002_sql, encoding="utf-8")

    with _disposable_db(pg_enabled) as conninfo:
        # Inicializar pool sobre la base descartable
        pool = create_pg_pool(conninfo, min_size=1, max_size=2, timeout=10.0)
        pool.wait(timeout=10.0)
        try:
            proxy = PgConnectionProxy(pool.getconn(), pool)
            try:
                # 1. Aplicar baseline 001
                statements = split_sql_statements(
                    (tmp_path / "001_initial_schema.sql").read_text(encoding="utf-8")
                )
                for stmt in statements:
                    stmt = stmt.strip()
                    if not stmt or stmt.upper() in ("BEGIN", "COMMIT", "ROLLBACK"):
                        continue
                    proxy.execute(stmt)
                proxy._conn.commit()

                # 2. Aplicar migración 002 vía proxy (ruta adaptada)
                statements_002 = split_sql_statements(migration_002_sql)
                for stmt in statements_002:
                    stmt = stmt.strip()
                    if not stmt or stmt.upper() in ("BEGIN", "COMMIT", "ROLLBACK"):
                        continue
                    proxy.execute(stmt)
                proxy._conn.commit()

                # Verificar valores almacenados
                with connect_autocommit(conninfo) as conn:
                    # 1. DEFAULT value
                    default = conn.execute(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name='d4_test' AND column_name='txt'"
                    ).fetchone()[0]
                    assert default is not None
                    # FIXED: proxy path now preserves % literal correctly
                    assert "valor%defecto" in default, (
                        f"DEFAULT almacenado incorrecto (proxy): {default!r}"
                    )

                    # 2. COMMENT
                    comment = conn.execute(
                        "SELECT obj_description('d4_test'::regclass)"
                    ).fetchone()[0]
                    assert comment is not None
                    # FIXED: proxy path now preserves % literal correctly
                    assert "Tabla con 100% cobertura" in comment, (
                        f"COMMENT almacenado incorrecto (proxy): {comment!r}"
                    )

                    # 3. Índice parcial con LIKE '%pattern%'
                    # PostgreSQL almacena LIKE como operador ~~ en pg_indexes.indexdef
                    idx_def = conn.execute(
                        "SELECT indexdef FROM pg_indexes WHERE indexname = 'd4_test_txt_like_idx'"
                    ).fetchone()[0]
                    assert idx_def is not None
                    # FIXED: proxy path now preserves % literal correctly
                    assert "txt ~~ '%pattern%'::text" in idx_def, (
                        f"Índice parcial almacenado incorrecto (proxy): {idx_def!r}"
                    )
            finally:
                pool.putconn(proxy._conn)
                pool.close()
        finally:
            pool.close()


def test_d4_percent_literal_in_parametrized_query(pg_enabled, tmp_path):
    """
    D4: Consulta parametrizada con LIKE '%...' a través de PgConnectionProxy.

    Verifica que los '%' en patrones LIKE con parámetros SÍ se dupliquen
    (protección para psycopg3), mientras que los '%' literales en consultas
    sin parámetros NO se dupliquen.
    """
    from database.pg_pool import PgConnectionProxy, create_pg_pool, split_sql_statements

    _baseline_copy(tmp_path)
    (tmp_path / "002_d4_percent.sql").write_text("", encoding="utf-8")

    with _disposable_db(pg_enabled) as conninfo:
        pool = create_pg_pool(conninfo, min_size=1, max_size=2, timeout=10.0)
        pool.wait(timeout=10.0)
        try:
            proxy = PgConnectionProxy(pool.getconn(), pool)
            try:
                # 1. Aplicar baseline 001
                statements = split_sql_statements(
                    (tmp_path / "001_initial_schema.sql").read_text(encoding="utf-8")
                )
                for stmt in statements:
                    stmt = stmt.strip()
                    if not stmt or stmt.upper() in ("BEGIN", "COMMIT", "ROLLBACK"):
                        continue
                    proxy.execute(stmt)
                proxy._conn.commit()

                # 2. Crear tabla de prueba
                proxy.execute("""
                    CREATE TABLE d4_param_test (
                        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                        txt TEXT
                    );
                """)
                proxy._conn.commit()

                # 3. Insertar con patrón LIKE parametrizado
                # El patrón '%test%' contiene % literales que SÍ deben duplicarse
                # porque la consulta tiene parámetros (psycopg3 los interpretaría como placeholders)
                cur = proxy.execute(
                    "INSERT INTO d4_param_test (txt) VALUES (%s)", ("prefijo%test%sufijo",)
                )
                proxy._conn.commit()

                # 4. Consultar con LIKE parametrizado
                cur = proxy.execute("SELECT txt FROM d4_param_test WHERE txt LIKE %s", ("%test%",))
                proxy._conn.commit()
                row = cur.fetchone()
                assert row is not None
                # El valor almacenado debe tener % literal, no %%
                assert row["txt"] == "prefijo%test%sufijo"

                # 5. Verificar que el patrón LIKE '%test%' en la consulta
                # se ejecutó correctamente (el % en el parámetro no se duplica en el valor almacenado)
                with connect_autocommit(conninfo) as conn:
                    rows = conn.execute(
                        "SELECT txt FROM d4_param_test WHERE txt LIKE %s", ("%test%",)
                    ).fetchall()
                    assert len(rows) == 1
                    # connect_autocommit devuelve tuplas (txt,)
                    assert rows[0][0] == "prefijo%test%sufijo"
            finally:
                pool.putconn(proxy._conn)
                pool.close()
        finally:
            pool.close()


def test_real_migrations_pg_apply_002_and_are_idempotent(pg_enabled):
    """Aplica las migraciones reales de migrations_pg/ (001 + 002)."""
    with _disposable_db(pg_enabled) as conninfo:
        result = pgm.run_migrations(conninfo)
        assert [item.version for item in result.applied] == [1, 2]
        assert result.current_version == 2
        with connect_autocommit(conninfo) as conn:
            cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'notification_log'"
                ).fetchall()
            }
            assert {"retry_count", "next_retry_at"} <= cols
            idxs = {
                r[0]
                for r in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'notification_log'"
                ).fetchall()
            }
            assert "idx_notification_log_retry" in idxs
            rows = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            assert rows == [(1, "001_initial_schema.sql"), (2, "002_notification_retry_policy.sql")]
        second = pgm.run_migrations(conninfo)
        assert second.applied == []
        assert second.current_version == 2
