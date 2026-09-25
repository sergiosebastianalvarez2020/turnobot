"""Configuración compartida de tests_pg (Fase 4H).

Sin ``TURNOBOT_PG_URL`` toda la suite hace SKIP limpio y la suite SQLite queda
intacta. Con la variable seteada, cada test vive en una base PostgreSQL
descartable (``turnobot_test_<uuid>``) sobre la que se aplica
``migrations_pg/001_initial_schema.sql`` (que ya contiene su propio
BEGIN/COMMIT y no es idempotente: se aplica UNA sola vez por base).
"""

import os

import pytest

from database.pg_pool import create_pg_pool
from tests_pg._helpers import (
    TURNOBOT_PG_URL_KEY,
    apply_initial_schema,
    create_test_database,
    drop_test_database,
    new_db_name,
    seed_business,
    test_db_conninfo,
)

TURNOBOT_PG_URL = os.getenv(TURNOBOT_PG_URL_KEY)


@pytest.fixture(autouse=True)
def pg_coherent_env(monkeypatch):
    """Coherencia de backend durante cada test de tests_pg.

    ``get_backend()`` (database.database) deriva de ``DATABASE_URL``; sin esa
    variable, ``acquire_business_write_lock`` degrada a no-op silencioso y los
    escenarios concurrentes perderían la serialización real que dicen probar.
    Este fixture (solo vive en tests_pg) fuerza el backend sobre la MISMA URL
    que usa el pool y lo restaura al final de cada test. No altera la suite
    SQLite ni producción.
    """
    monkeypatch.setenv("DATABASE_URL", TURNOBOT_PG_URL or "")
    monkeypatch.setenv("DB_BACKEND", "postgresql")


@pytest.fixture(scope="session")
def pg_enabled() -> str:
    """URL de mantenimiento del PostgreSQL real; SKIP limpio si no está definida."""
    if not TURNOBOT_PG_URL:
        pytest.skip("TURNOBOT_PG_URL no está definida; suite PostgreSQL (4H) omitida.")
    return TURNOBOT_PG_URL


@pytest.fixture
def pg_test_database(pg_enabled: str):
    """Crea una base descartable por test y la destruye al finalizar."""
    dbname = new_db_name()
    create_test_database(pg_enabled, dbname)
    test_url = test_db_conninfo(pg_enabled, dbname)
    apply_initial_schema(test_url)
    try:
        yield test_url
    finally:
        drop_test_database(pg_enabled, dbname)


@pytest.fixture
def pg_pool(pg_test_database: str):
    """Pool REAL psycopg_pool (min_size>=2) sobre la base descartable."""
    pool = create_pg_pool(pg_test_database, min_size=2, max_size=8, timeout=20.0, open=True)
    try:
        pool.wait(timeout=15.0)
    except Exception:
        pool.close(timeout=5.0)
        raise
    try:
        yield pool
    finally:
        pool.close(timeout=15.0)


@pytest.fixture
def pg_seed(pg_test_database: str) -> dict:
    """Crea un negocio mínimo y devuelve ``{"business_id", "url"}``."""
    business_id = seed_business(pg_test_database)
    return {"business_id": business_id, "url": pg_test_database}
