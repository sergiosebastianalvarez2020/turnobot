"""Pruebas D14: concurrencia entre runner y bootstrap (advisory locks).

Valida el cruce entre el lock de sesión del migrador
``pg_try_advisory_lock`` (database/pg_migrator.py::_acquire_lock) y el lock de
transacción del bootstrap ``pg_advisory_xact_lock``
(database/database.py::_init_postgresql).

Según docs/PG_MIGRATOR_AUDIT_BASELINE.md, sección 3.3 / D14, la intersección
sesión/transacción no estaba cubierta por la cobertura existente: las pruebas
de timeout usaban ``pg_advisory_lock`` (sesión contra sesión). Aquí se ejerce
el cruce con un PostgreSQL real y una base descartable.
"""

import psycopg
import pytest

from tests_pg._helpers import (
    apply_initial_schema,
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


def _metadata_table_exists(conninfo: str) -> bool:
    with connect_autocommit(conninfo) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.schema_migrations')"
        ).fetchone()
    return bool(row and row[0])


def test_advisory_lock_session_transaction_crossover(pg_enabled: str) -> None:
    """D14: winner bloquea en pg_advisory_xact_lock y runner aguarda/recae.

    Secuencia:
    1. Base descartable con schema inicial aplicado.
    2. Bloqueador (transacción abierta) toma ``pg_advisory_xact_lock``.
    3. Runner con ``lock_timeout=0.5`` -> ``MigrationLockTimeout`` y sin escribir.
    4. Se libera el lock -> runner adopta y aplica baseline.
    """
    from pathlib import Path

    import database.pg_migrator as pgm

    dbname = new_db_name()
    tmp_dir = Path.cwd() / "d14_migrations_tmp"
    tmp_dir.mkdir(exist_ok=True)
    try:
        # Reset del directorio de migraciones de la prueba (solo baseline 001).
        for old in tmp_dir.glob("*.sql"):
            old.unlink()
        (tmp_dir / "001_initial_schema.sql").write_text(
            (pgm.MIGRATIONS_PG_DIR / "001_initial_schema.sql").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        create_test_database(pg_enabled, dbname)
        test_url = build_test_db_conninfo(pg_enabled, dbname)
        apply_initial_schema(test_url)

        # 1. Bloqueador: lock en transacción abierta.
        blocker = connect_autocommit(test_url)
        try:
            blocker.execute("BEGIN")
            blocker.execute(
                "SELECT pg_advisory_xact_lock(%s)", (pgm._PG_SCHEMA_LOCK,)
            )

            # 2. Runner con espera explícita y corta (sobre una conexión real).
            runner = psycopg.connect(test_url)
            try:
                with pytest.raises(pgm.MigrationLockTimeout, match="advisory lock"):
                    pgm.apply_pending_migrations(
                        conn=runner,
                        migrations_dir=tmp_dir,
                        lock_timeout=0.5,
                    )
            finally:
                runner.close()

            # 3. Sin escritura: la metadata del migrator no se creó con el runner
            # paralelo que no pudo adquirir el lock transaccional.
            assert _metadata_table_exists(test_url) is False
        finally:
            try:
                blocker.execute("ROLLBACK")
            except Exception:
                pass
            blocker.close()

        # 4. Recuperación: reejecutar y adoptar baseline (001 ya estaba en el
        # esquema, así que se registra sin ejecutar).
        runner = psycopg.connect(test_url)
        try:
            result = pgm.apply_pending_migrations(
                conn=runner,
                migrations_dir=tmp_dir,
                lock_timeout=None,
            )
        finally:
            runner.close()
        # Auto-baseline: adopta la versión 1 sin ejecutar (ya estaba en el esquema).
        assert [item.version for item in result.adopted] == [1]
        assert [item.version for item in result.applied] == []
        assert _metadata_table_exists(test_url) is True
    finally:
        drop_test_database(pg_enabled, dbname)
        for old in tmp_dir.glob("*.sql"):
            old.unlink()
        tmp_dir.rmdir()
