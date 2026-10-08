"""Configuración de tests funcionales — PostgreSQL aislado por test.

Esta suite usa el mismo mecanismo validado de tests_pg/conftest.py:
- Requiere TURNOBOT_PG_URL (PostgreSQL 16 en 127.0.0.1:5433)
- Cada test obtiene una base temporal turnobot_test_<uuid>
- Se aplica migrations_pg/001_initial_schema.sql una sola vez por base
- La base se destruye al finalizar el test (incluso ante fallo)

Tests que necesiten SQLite por razón legítima (migración, legacy explícito)
deben sobrescribir el backend en el propio test, no aquí.
"""

import atexit
import os

import pytest

from tests_pg._helpers import (
    TURNOBOT_PG_URL_KEY,
    apply_initial_schema,
    build_test_db_conninfo,
    conninfo_to_url,
    create_test_database,
    drop_test_database,
    new_db_name,
    seed_standard_test_data,
)

TURNOBOT_PG_URL = os.getenv(TURNOBOT_PG_URL_KEY)


# Create a default test database at session start for module-level imports
_default_test_db_url: str | None = None
_default_test_db_name: str | None = None


def _drop_default_test_database() -> None:
    """Elimina la base de sesión. Idempotente.

    `drop_test_database` usa `DROP DATABASE IF EXISTS ... WITH (FORCE)`, así que
    llamarla más de una vez es seguro. La invocan dos caminos: el fixture de
    sesión y `atexit`. pytest no ejecuta fixtures cuando la sesión termina sin
    correr ningún test (`--collect-only`, error de colección, interrupción), y
    sin `atexit` esa base quedaba huérfana para siempre.
    """
    global _default_test_db_name
    if not _default_test_db_name:
        return
    try:
        drop_test_database(TURNOBOT_PG_URL, _default_test_db_name)
    finally:
        _default_test_db_name = None


def _create_default_test_database() -> str:
    """Create a default test database for module-level imports."""
    global _default_test_db_url, _default_test_db_name
    if not TURNOBOT_PG_URL:
        return ""
    dbname = new_db_name()
    try:
        create_test_database(TURNOBOT_PG_URL, dbname)
        test_url = build_test_db_conninfo(TURNOBOT_PG_URL, dbname)
        apply_initial_schema(test_url)
        seed_standard_test_data(test_url)
    except BaseException:
        # No abandonar la base recién creada si falla el schema o el seed.
        drop_test_database(TURNOBOT_PG_URL, dbname)
        raise
    _default_test_db_name = dbname
    _default_test_db_url = test_url
    return conninfo_to_url(test_url)


if TURNOBOT_PG_URL:
    os.environ.setdefault("DATABASE_URL", _create_default_test_database())
    os.environ.setdefault("DB_BACKEND", "postgresql")
    os.environ.setdefault("FLASK_ENV", "development")
    atexit.register(_drop_default_test_database)


@pytest.fixture(autouse=True)
def pg_test_env(monkeypatch):
    """Configura el entorno para que la aplicación use PostgreSQL de tests.

    Forza DATABASE_URL y DB_BACKEND a PostgreSQL. El pool del módulo `app`
    se reconfigura a una base temporal por test mediante `pg_per_test_db`.
    """
    if not TURNOBOT_PG_URL:
        pytest.skip("TURNOBOT_PG_URL no está definida; suite funcional PostgreSQL omitida.")
    monkeypatch.setenv("DATABASE_URL", TURNOBOT_PG_URL)
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("FLASK_ENV", "development")


@pytest.fixture(scope="session", autouse=True)
def cleanup_default_test_database():
    """Clean up the default test database at session end."""
    yield
    _drop_default_test_database()


@pytest.fixture(scope="session")
def pg_maintenance_url() -> str:
    """URL de mantenimiento del PostgreSQL real."""
    if not TURNOBOT_PG_URL:
        pytest.skip("TURNOBOT_PG_URL no está definida; suite PostgreSQL (4H) omitida.")
    return TURNOBOT_PG_URL


@pytest.fixture
def pg_test_database(pg_maintenance_url: str):
    """Crea una base descartable por test y la destruye al finalizar.

    La creación va DENTRO del `try`: si `apply_initial_schema` o
    `seed_standard_test_data` fallan, la base recién creada se destruye igual en
    lugar de quedar huérfana.
    """
    dbname = new_db_name()
    try:
        create_test_database(pg_maintenance_url, dbname)
        test_url = build_test_db_conninfo(pg_maintenance_url, dbname)
        apply_initial_schema(test_url)
        seed_standard_test_data(test_url)
        yield test_url
    finally:
        drop_test_database(pg_maintenance_url, dbname)


@pytest.fixture
def pg_test_url(pg_test_database: str) -> str:
    """Alias semántico: URL de la base de tests lista para usar."""
    return pg_test_database


@pytest.fixture
def app(pg_test_database: str, monkeypatch):
    """Crea la aplicación Flask configurada contra la base de tests temporal.

    Usa la factory create_app() que inicializa el pool PostgreSQL.
    Cada test que requestee este fixture recibe su propia app aislada.

    NO pusha app_context: cada test debe manejar su propio contexto para
    garantizar aislamiento de flask.g entre request contexts.
    """
    test_url = conninfo_to_url(pg_test_database)
    monkeypatch.setenv("DATABASE_URL", test_url)
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("FLASK_ENV", "development")

    from application import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def app_ctx(app):
    """Push an app context for the test, auto-pops at teardown."""
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    """Cliente de test Flask."""
    return app.test_client()


@pytest.fixture
def seed_business(pg_test_database: str) -> int:
    """Crea un negocio mínimo en la base de tests y devuelve su ID."""
    from tests_pg._helpers import seed_business

    test_url = conninfo_to_url(pg_test_database)
    return seed_business(test_url)


@pytest.fixture
def seed_business_with_settings(pg_test_database: str, seed_business: int) -> int:
    """Crea un negocio con configuración completa (horarios, servicios)."""
    from tests_pg._helpers import seed_business_settings, seed_full_week, seed_service

    test_url = conninfo_to_url(pg_test_database)
    seed_business_settings(test_url, seed_business)
    seed_full_week(test_url, seed_business)
    seed_service(test_url, seed_business)
    return seed_business


@pytest.fixture(autouse=True)
def pg_isolate_module_app(request):
    """Reconfigura el pool del module-level app (application.app) a la base de tests.

    Tests unittest que usan `import app as application; application.app.test_client()`
    necesitan el pool del módulo apuntando a una base aislada. Este fixture:
    1. Crea una base temporal
    2. Aplica schema + seed
    3. Cierra el pool del module-level app
    4. Crea un nuevo pool con la base temporal
    5. Al terminar, destruye la base y restaura el pool original

    NOTA: Tests que usan el fixture `app` (per-test) no se ven afectados porque
    ese fixture crea una app nueva con su propio pool.
    """
    if not TURNOBOT_PG_URL:
        yield
        return

    # Los tests que solicitan el fixture `app` ya reciben una instancia y una
    # base PostgreSQL propias. No hace falta crear además otra base para la
    # fachada global importada por compatibilidad (por ejemplo, para parches).
    if "app" in request.fixturenames:
        yield
        return

    import app as application_mod
    from database.pg_pool import close_pg_pool, init_pg_pool

    # Create a per-test database
    dbname = new_db_name()
    created = False
    try:
        create_test_database(TURNOBOT_PG_URL, dbname)
        created = True
        test_conninfo = build_test_db_conninfo(TURNOBOT_PG_URL, dbname)
        apply_initial_schema(test_conninfo)
        seed_standard_test_data(test_conninfo)
        test_url = conninfo_to_url(test_conninfo)

        # Save old pool state
        old_pool = application_mod.app.extensions.get("pg_pool")

        # Close existing pool
        if old_pool:
            close_pg_pool(app=application_mod.app)

        # Create new pool pointing to per-test database
        os.environ["DATABASE_URL"] = test_url
        application_mod.app.config["DATABASE_URL"] = test_url
        new_pool = init_pg_pool(application_mod.app)
        application_mod.app.extensions["pg_pool"] = new_pool

        yield
    finally:
        # El teardown va en dos niveles: si CUALQUIER paso falla (cerrar el pool,
        # restaurar el pool original), el `finally` interno sigue ejecutando el
        # drop. Antes un fallo en la restauracion saltaba el drop y dejaba la
        # base huerfana para siempre. `drop_test_database` usa IF EXISTS, asi que
        # es idempotente y seguro invocarlo aunque la creacion haya fallado.
        try:
            if created:
                # Close per-test pool
                close_pg_pool(app=application_mod.app)

                # Restore pool to default test database
                os.environ["DATABASE_URL"] = (
                    _default_test_db_url if _default_test_db_url else TURNOBOT_PG_URL
                )
                if _default_test_db_url:
                    # Parse the URL back to conninfo for init_pg_pool
                    application_mod.app.config["DATABASE_URL"] = os.environ["DATABASE_URL"]
                    restored_pool = init_pg_pool(application_mod.app)
                    application_mod.app.extensions["pg_pool"] = restored_pool
        finally:
            # Drop the per-test database
            drop_test_database(TURNOBOT_PG_URL, dbname)
