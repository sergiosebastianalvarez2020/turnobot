"""Configuración de tests funcionales — PostgreSQL aislado por test.

Esta suite usa el mismo mecanismo validado de tests_pg/conftest.py:
- Requiere TURNOBOT_PG_URL (PostgreSQL 16 en 127.0.0.1:5433)
- Cada test obtiene una base temporal turnobot_test_<uuid>
- Se aplica el baseline y las migraciones PostgreSQL actuales en cada base
- La base se destruye al finalizar el test (incluso ante fallo)

Tests que necesiten SQLite por razón legítima (migración, legacy explícito)
deben sobrescribir el backend en el propio test, no aquí.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import psycopg
import pytest

from database import database as _database_module
from tests_pg._helpers import (
    TURNOBOT_PG_URL_KEY,
    apply_current_schema,
    build_test_db_conninfo,
    conninfo_to_url,
    create_test_database,
    drop_test_database,
    new_db_name,
    seed_standard_test_data,
)

TURNOBOT_PG_URL = os.getenv(TURNOBOT_PG_URL_KEY)
_REAL_PSYCOPG_CONNECT = psycopg.connect


# Default test database: created lazily on first use, never at import time.
# This ensures tests marked `no_pg` can run even when PostgreSQL is unavailable.
_default_test_db_url: str | None = None
_default_test_db_name: str | None = None


def _drop_default_test_database() -> None:
    """Elimina la base de datos de sesión por defecto. Idempotente."""
    global _default_test_db_name
    if not _default_test_db_name:
        return
    try:
        drop_test_database(TURNOBOT_PG_URL, _default_test_db_name)
    finally:
        _default_test_db_name = None


def _ensure_default_test_database() -> str:
    """Crea la base de datos de prueba por defecto bajo demanda.

    Solo se invoca cuando un test realmente necesita PostgreSQL (fixture
    `pg_test_env` para tests no-`no_pg`, o `pg_isolate_module_app`).
    Nunca se ejecuta durante la importación del módulo.

    Solo se omiten (skip) los errores de conexión a PostgreSQL
    (servidor no disponible, host no resoluble, puerto cerrado).
    Errores de esquema, semillas, permisos o configuración se dejan
    propagar como fallos reales de test para no ocultar problemas.
    """
    global _default_test_db_url, _default_test_db_name
    if _default_test_db_url:
        return _default_test_db_url
    if not TURNOBOT_PG_URL:
        return ""
    dbname = new_db_name()
    try:
        create_test_database(TURNOBOT_PG_URL, dbname)
        test_url = build_test_db_conninfo(TURNOBOT_PG_URL, dbname)
        apply_current_schema(test_url)
        seed_standard_test_data(test_url)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # PostgreSQL no disponible: error de conexión, no de configuración.
        try:
            drop_test_database(TURNOBOT_PG_URL, dbname)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            pass
        return ""
    _default_test_db_name = dbname
    _default_test_db_url = conninfo_to_url(test_url)
    return _default_test_db_url


def _drop_test_database_with_real_connection(base_url: str, dbname: str) -> None:
    """Elimina la base aunque el test haya parcheado ``psycopg.connect``.

    La limpieza de infraestructura no debe quedar sujeta a mocks funcionales:
    algunos tests parchean ``psycopg.connect`` para verificar que el migrador
    no conecta, y ese parche sigue activo al ejecutarse el teardown del fixture.
    """
    patched_connect = psycopg.connect
    psycopg.connect = _REAL_PSYCOPG_CONNECT
    try:
        drop_test_database(base_url, dbname)
    finally:
        psycopg.connect = patched_connect


# `app.py` creates the application at import time. Keep that import on an
# isolated SQLite file even when PostgreSQL tests are configured; fixtures
# switch to per-test PostgreSQL databases only after collection.
_sqlite_import_dir = tempfile.mkdtemp(prefix="turnobot_import_sqlite_")
_database_module.DATABASE_PATH = Path(_sqlite_import_dir) / "appointments.db"
os.environ["DATABASE_URL"] = ""
os.environ["DB_BACKEND"] = "sqlite"
os.environ["FLASK_ENV"] = "development"


def _cleanup_import_sqlite_dir() -> None:
    shutil.rmtree(_sqlite_import_dir, ignore_errors=True)


atexit.register(_cleanup_import_sqlite_dir)
if TURNOBOT_PG_URL:
    atexit.register(_drop_default_test_database)


_MISSING = object()
_APP_DB_CONFIG_KEYS = ("DB_BACKEND", "DATABASE_URL")


def _module_app():
    """Devuelve `application.app` si el módulo ya fue importado, sin importarlo."""
    application_mod = sys.modules.get("app")
    return getattr(application_mod, "app", None)


def _snapshot_app_config(application_app):
    """Guarda los valores de configuración de DB, incluida su ausencia."""
    if application_app is None:
        return None
    return {key: application_app.config.get(key, _MISSING) for key in _APP_DB_CONFIG_KEYS}


def _restore_app_config(application_app, snapshot):
    """Restaura exactamente los valores de configuración guardados."""
    if application_app is None or snapshot is None:
        return
    for key, value in snapshot.items():
        if value is _MISSING:
            application_app.config.pop(key, None)
        else:
            application_app.config[key] = value


def _pool_is_usable(pool) -> bool:
    """Indica si un pool puede restaurarse sin dejar una referencia inválida."""
    return pool is not None and not getattr(pool, "closed", False)


def _align_sqlite_config(application_app) -> None:
    """Alinea la configuración del módulo `app` al backend SQLite."""
    if application_app is not None:
        application_app.config["DATABASE_URL"] = ""
        application_app.config["DB_BACKEND"] = "sqlite"


@pytest.fixture(autouse=True)
def pg_test_env(request, monkeypatch):
    """Configura el entorno para que la aplicación use PostgreSQL de tests.

    Forza DATABASE_URL y DB_BACKEND a PostgreSQL. El pool del módulo `app`
    se reconfigura a una base temporal por test mediante `pg_per_test_db`.

    Los tests marcados con `no_pg` no requieren base de datos y se ejecutan
    sin inicializar PostgreSQL ni saltar. Aun así, se alinean el entorno y la
    configuración del módulo `app` a SQLite para que ninguna ruta ni llamada
    fuera de contexto use un `DATABASE_URL` PostgreSQL heredado de otro test.
    """
    if request.node.get_closest_marker("no_pg"):
        application_app = _module_app()
        config_snapshot = _snapshot_app_config(application_app)
        monkeypatch.setenv("DB_BACKEND", "sqlite")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("FLASK_ENV", "development")
        _align_sqlite_config(application_app)
        try:
            yield
        finally:
            _restore_app_config(application_app, config_snapshot)
        return
    if not TURNOBOT_PG_URL:
        pytest.skip("TURNOBOT_PG_URL no está definida; suite funcional PostgreSQL omitida.")
    # Crear la base de datos de prueba por defecto bajo demanda (nunca en importación).
    test_db_url = _ensure_default_test_database()
    if not test_db_url:
        pytest.skip("No se pudo crear la base de datos de prueba por defecto.")
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("FLASK_ENV", "development")
    yield


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

    La creación va DENTRO del `try`: si la aplicación del esquema o
    `seed_standard_test_data` fallan, la base recién creada se destruye igual en
    lugar de quedar huérfana.
    """
    dbname = new_db_name()
    try:
        create_test_database(pg_maintenance_url, dbname)
        test_url = build_test_db_conninfo(pg_maintenance_url, dbname)
        apply_current_schema(test_url)
        seed_standard_test_data(test_url)
        yield test_url
    finally:
        _drop_test_database_with_real_connection(pg_maintenance_url, dbname)


@pytest.fixture
def pg_test_url(pg_test_database: str) -> str:
    """Alias semántico: URL de la base de tests lista para usar."""
    return pg_test_database


@pytest.fixture
def sqlite_temp_db(request, monkeypatch):
    """Crea una base SQLite temporal e independiente por test.

    Configura DATABASE_PATH, DB_BACKEND, DATABASE_URL, FLASK_ENV y la
    configuración del módulo `app` para usar la base temporal, inicializa el
    esquema mediante migraciones y restaura todo el estado global al finalizar.

    No interfiere con pg_test_env, pg_test_database ni pg_isolate_module_app:
    los tests marcados con no_pg simplemente no activan esos fixtures PG.
    """
    import database.database as database

    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "appointments.db"
    application_app = _module_app()

    # Guardar estado original antes de modificar nada.
    orig_db_path = database.DATABASE_PATH
    config_snapshot = _snapshot_app_config(application_app)

    try:
        # Configurar entorno y configuración Flask de forma coherente.
        database.DATABASE_PATH = db_path
        monkeypatch.setenv("DB_BACKEND", "sqlite")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("FLASK_ENV", "development")
        _align_sqlite_config(application_app)

        # Inicializar esquema (migraciones crean negocio 1, servicios, horarios, etc.)
        database.init_database()

        yield db_path
    finally:
        # Restaurar el estado aunque el test o la inicialización fallen.
        database.DATABASE_PATH = orig_db_path
        _restore_app_config(application_app, config_snapshot)
        temp_dir.cleanup()


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
def pg_isolate_module_app(request, monkeypatch):
    """Reconfigura el pool del module-level app (application.app) a la base de tests.

    Tests unittest que usan `import app as application; application.app.test_client()`
    necesitan el pool del módulo apuntando a una base aislada. Este fixture:
    1. Crea una base temporal
    2. Aplica schema + seed
    3. Aparta (sin cerrarlo) el pool anterior del module-level app
    4. Crea un nuevo pool con la base temporal
    5. Al terminar, cierra el pool temporal, destruye la base y restaura
       configuración, DATABASE_PATH, entorno y referencias de pool originales.

    NOTA: Tests que usan el fixture `app` (per-test) no se ven afectados porque
    ese fixture crea una app nueva con su propio pool.
    """
    if not TURNOBOT_PG_URL:
        if not request.node.get_closest_marker("no_pg"):
            pytest.skip("TURNOBOT_PG_URL no está definida; se requiere PostgreSQL.")
        yield
        return

    # Tests marked with no_pg must not connect to PostgreSQL
    if request.node.get_closest_marker("no_pg"):
        yield
        return

    # Los tests que solicitan el fixture `app` ya reciben una instancia y una
    # base PostgreSQL propias. No hace falta crear además otra base para la
    # fachada global importada por compatibilidad (por ejemplo, para parches).
    if "app" in request.fixturenames:
        yield
        return

    import app as application_mod
    import database.database as database
    from database import pg_pool
    from database.pg_pool import close_pg_pool, init_pg_pool

    # Create a per-test database
    dbname = new_db_name()
    try:
        create_test_database(TURNOBOT_PG_URL, dbname)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # PostgreSQL no disponible: el test no puede ejecutarse sin una base real.
        pytest.skip("PostgreSQL no está disponible; no se pudo crear la base de datos de prueba.")

    created = True
    application_app = application_mod.app
    orig_db_path = database.DATABASE_PATH
    config_snapshot = _snapshot_app_config(application_app)
    old_pool = application_app.extensions.get("pg_pool")
    orig_global_pool = pg_pool._global_pool
    orig_global_pool_app_owned = pg_pool._global_pool_app_owned
    pool_swapped = False
    try:
        test_conninfo = build_test_db_conninfo(TURNOBOT_PG_URL, dbname)
        apply_current_schema(test_conninfo)
        seed_standard_test_data(test_conninfo)
        test_url = conninfo_to_url(test_conninfo)

        # Apartar el pool anterior sin cerrarlo: puede pertenecer a otro test.
        if old_pool is not None:
            application_app.extensions.pop("pg_pool", None)
        pool_swapped = True

        # Create new pool pointing to per-test database
        monkeypatch.setenv("DATABASE_URL", test_url)
        application_app.config["DATABASE_URL"] = test_url
        application_app.config["DB_BACKEND"] = "postgresql"
        new_pool = init_pg_pool(application_app)
        if new_pool is None:
            raise RuntimeError("init_pg_pool no creó el pool PostgreSQL para el test")
        application_app.extensions["pg_pool"] = new_pool

        yield
    finally:
        # El teardown va en dos niveles: si CUALQUIER paso falla (cerrar el pool,
        # restaurar el estado original), el `finally` interno sigue ejecutando el
        # drop. Antes un fallo en la restauracion saltaba el drop y dejaba la
        # base huerfana para siempre. `drop_test_database` usa IF EXISTS, asi que
        # es idempotente y seguro invocarlo aunque la creacion haya fallado.
        try:
            if created:
                if pool_swapped:
                    # Cerrar SOLO el pool temporal de esta app (nunca el global
                    # ni el pool anterior, que fue apartado sin cerrar).
                    try:
                        close_pg_pool(app=application_app)
                    finally:
                        application_app.extensions.pop("pg_pool", None)

                # Restaurar configuración, ruta SQLite y referencias originales.
                database.DATABASE_PATH = orig_db_path
                _restore_app_config(application_app, config_snapshot)

                if _pool_is_usable(old_pool):
                    application_app.extensions["pg_pool"] = old_pool
                if _pool_is_usable(orig_global_pool):
                    pg_pool._global_pool = orig_global_pool
                    pg_pool._global_pool_app_owned = orig_global_pool_app_owned
                else:
                    pg_pool._global_pool = None
                    pg_pool._global_pool_app_owned = False
        finally:
            # Drop the per-test database
            _drop_test_database_with_real_connection(TURNOBOT_PG_URL, dbname)
