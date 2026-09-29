"""Tests de hardening de infraestructura PostgreSQL (pool, sesión, teardown).

Estos tests NO requieren un PostgreSQL real: los pools se crean con
``open=False`` y las conexiones se falsean con dobles explícitos. Complementan
la suite de integración real (`tests_pg/`), que es la que valida el
comportamiento efectivo contra el servidor.

Cubren:

- FASE 1/2 — fuente única de verdad del pool: ``app.extensions["pg_pool"]`` se
  resuelve desde el contexto de aplicación Flask, desde fuera de él y a través de
  ``get_connection()`` (regresión del RuntimeError en producción); sin pools
  duplicados; SQLite intacto.
- FASE 3/8 — configuración de sesión obligatoria (timezone=UTC, lock_timeout)
  aplicada a cada conexión nueva del pool.
- FASE 5/6 — teardown del pool ligado al ciclo de vida de Flask: cierra el pool,
  es idempotente, no afecta a otras instancias ni a SQLite.
"""

from __future__ import annotations

import atexit
import importlib
import sqlite3

import pytest
from flask import Flask

import database.database as dbmod
import database.pg_pool as pg_pool_mod
from database.pg_pool import (
    PG_LOCK_TIMEOUT,
    PG_SESSION_TIMEZONE,
    PgConnectionProxy,
    close_pg_pool,
    configure_pg_session,
    create_pg_pool,
    get_pg_pool,
    init_pg_pool,
    register_pg_pool_teardown,
)

_PG_URL = "postgresql://usr:pwd@localhost:5432/turnobot_test"
_SQLITE_URL = "sqlite:///database/appointments.db"


class _FakeConn:
    """Conexión psycopg mínima que registra la configuración de sesión aplicada."""

    def __init__(self) -> None:
        self.autocommit = False
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def close(self) -> None:
        self.closed = True


class _FakePool:
    """Pool mínimo con la misma superficie que usa PgConnectionProxy."""

    def __init__(self, conninfo=None, **kwargs) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.closed = False
        self.close_timeouts: list[float] = []
        self.checkouts: list[_FakeConn] = []
        self.putbacks: list[_FakeConn] = []
        self._idle = [_FakeConn() for _ in range(4)]

    def getconn(self) -> _FakeConn:
        connection = self._idle.pop(0)
        self.checkouts.append(connection)
        return connection

    def putconn(self, connection) -> None:
        self.putbacks.append(connection)
        self._idle.append(connection)

    def close(self, timeout: float = 5.0) -> None:
        self.closed = True
        self.close_timeouts.append(timeout)


@pytest.fixture(autouse=True)
def _no_global_pool_leak():
    """Cada test arranca y termina sin pool global (evita fugas entre tests)."""
    close_pg_pool()
    yield
    close_pg_pool()


@pytest.fixture
def pg_app() -> Flask:
    """App Flask con backend PostgreSQL, sin pool todavía."""
    app = Flask("pg_hardening")
    app.config["DATABASE_URL"] = _PG_URL
    app.config["DB_BACKEND"] = "postgresql"
    return app


def _init_fake_pool(app: Flask) -> _FakePool:
    """Registra un pool falso como si lo hubiera creado init_pg_pool()."""
    fake = _FakePool(_PG_URL)
    app.extensions["pg_pool"] = fake
    pg_pool_mod._global_pool = fake
    return fake


# ============================================================
# FASE 1/2 — FUENTE ÚNICA DE VERDAD DEL POOL
# ============================================================


def test_get_pg_pool_resuelve_desde_el_contexto_de_aplicacion(pg_app):
    """Con contexto Flask activo, get_pg_pool() SIN argumentos devuelve el de la app."""
    fake = _init_fake_pool(pg_app)

    with pg_app.app_context():
        assert get_pg_pool() is fake
        assert get_pg_pool(pg_app) is fake


def test_get_pg_pool_fuera_del_contexto_usa_el_mismo_pool(pg_app):
    """Sin contexto Flask se resuelve el MISMO objeto, no una segunda copia del pool."""
    fake = _init_fake_pool(pg_app)

    assert get_pg_pool() is fake
    assert pg_pool_mod._global_pool is fake


def test_app_explicita_tiene_prioridad_sobre_el_contexto_actual():
    """La app pasada como argumento manda sobre la del contexto activo."""
    app_explicita = Flask("pg_explicita")
    app_contexto = Flask("pg_contexto")
    pool_explicito = _FakePool(_PG_URL)
    pool_contexto = _FakePool(_PG_URL)
    app_explicita.extensions["pg_pool"] = pool_explicito
    app_contexto.extensions["pg_pool"] = pool_contexto

    with app_contexto.app_context():
        assert get_pg_pool(app_explicita) is pool_explicito
        assert get_pg_pool() is pool_contexto


def test_init_pg_pool_crea_un_solo_pool_por_app(pg_app, monkeypatch):
    """Re-entradas de init_pg_pool() reutilizan el pool: nunca crean un segundo."""
    creados: list[_FakePool] = []

    def fake_create(conninfo, **kwargs):
        pool = _FakePool(conninfo, **kwargs)
        creados.append(pool)
        return pool

    monkeypatch.setattr(pg_pool_mod, "create_pg_pool", fake_create)

    primero = init_pg_pool(pg_app, open=False)
    segundo = init_pg_pool(pg_app, open=False)
    tercero = init_pg_pool(pg_app, open=False)

    assert primero is not None
    assert primero is segundo is tercero
    assert len(creados) == 1
    assert pg_app.extensions["pg_pool"] is primero
    assert pg_pool_mod._global_pool is primero


def test_init_pg_pool_reinicializa_despues_del_teardown(pg_app, monkeypatch):
    """Tras cerrar el pool, init_pg_pool() crea uno nuevo (el viejo sale de extensions)."""
    pools: list[_FakePool] = []
    monkeypatch.setattr(
        pg_pool_mod,
        "create_pg_pool",
        lambda conninfo, **kw: pools.append(_FakePool(conninfo)) or pools[-1],
    )

    primero = init_pg_pool(pg_app, open=False)
    close_pg_pool(app=pg_app)
    segundo = init_pg_pool(pg_app, open=False)

    assert primero is not None and segundo is not None
    assert primero is not segundo
    assert len(pools) == 2
    assert pg_app.extensions["pg_pool"] is segundo


def test_init_pg_pool_no_crea_pool_con_sqlite():
    """Con backend SQLite no hay pool, ni en extensions ni en el respaldo global."""
    app = Flask("sqlite_app")
    app.config["DATABASE_URL"] = _SQLITE_URL
    app.config["DB_BACKEND"] = "sqlite"

    assert init_pg_pool(app, open=False) is None
    assert "pg_pool" not in app.extensions
    assert get_pg_pool(app) is None
    assert get_pg_pool() is None


def test_get_connection_devuelve_conexion_del_pool_registrado(pg_app, monkeypatch):
    """get_connection() en contexto de app entrega una conexión del pool de la app."""
    fake = _init_fake_pool(pg_app)
    monkeypatch.setenv("DATABASE_URL", _PG_URL)

    with pg_app.app_context():
        connection = dbmod.get_connection()

        assert isinstance(connection, PgConnectionProxy)
        assert connection._pool is fake
        assert fake.checkouts == [connection._conn]

        connection.close()
        assert fake.putbacks == [connection._conn]


def test_get_connection_fuera_de_contexto_no_levanta_runtime_error(pg_app, monkeypatch):
    """Regresión del bug app.extensions/_global_pool: get_connection() sin contexto
    Flask debe encontrar el pool de create_app() y noraising RuntimeError."""
    fake = _init_fake_pool(pg_app)
    monkeypatch.setenv("DATABASE_URL", _PG_URL)

    connection = dbmod.get_connection()
    try:
        assert isinstance(connection, PgConnectionProxy)
        assert connection._pool is fake
    finally:
        connection.close()


def test_get_connection_sin_pool_sigue_levantando_runtime_error(monkeypatch):
    """Sin pool no hay conexión: el contrato de error se mantiene."""
    monkeypatch.setenv("DATABASE_URL", _PG_URL)

    with pytest.raises(RuntimeError, match="no está inicializado"):
        dbmod.get_connection()


def test_sqlite_sigue_funcionando_exactamente_igual(monkeypatch, tmp_path):
    """SQLite no se ve afectado: sqlite3.Row, PRAGMAs y ausencia de pool."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(dbmod, "DATABASE_PATH", tmp_path / "appointments.db")

    connection = dbmod.get_connection()
    try:
        assert isinstance(connection, sqlite3.Connection)
        assert connection.row_factory is sqlite3.Row
        assert get_pg_pool() is None
        assert pg_pool_mod._global_pool is None
    finally:
        connection.close()


# ============================================================
# FASE 3/8 — CONFIGURACIÓN DE SESIÓN (timezone + lock_timeout)
# ============================================================


def test_configure_pg_session_fija_timezone_utc_y_lock_timeout():
    """Cada conexión nueva recibe timezone=UTC y lock_timeout con valores fijos."""
    connection = _FakeConn()

    configure_pg_session(connection)

    assert connection.executed == [
        ("SELECT set_config('TimeZone', %s, false)", ("UTC",)),
        ("SELECT set_config('lock_timeout', %s, false)", ("5s",)),
    ]
    # Los set_config(.., false) son de SESIÓN: se aplican en autocommit y no
    # pueden quedar pendientes de un COMMIT posterior de la aplicación.
    assert connection.autocommit is False


def test_constantes_de_sesion_documentan_los_valores_esperados():
    """Los valores por defecto son los que los tests de integración verifican."""
    assert PG_SESSION_TIMEZONE == "UTC"
    assert PG_LOCK_TIMEOUT == "5s"


def test_create_pg_pool_registra_el_configurador_de_sesion(monkeypatch):
    """El pool recibe `configure`: psycopg lo aplica a cada conexión nueva."""
    captured: dict = {}

    def fake_pool_class(**kwargs):
        captured.update(kwargs)
        return _FakePool(kwargs.get("conninfo"))

    monkeypatch.setattr(pg_pool_mod.psycopg_pool, "ConnectionPool", fake_pool_class)

    create_pg_pool(_PG_URL, open=False)

    assert captured["configure"] is configure_pg_session


def test_create_pg_pool_respeta_un_configurador_propio(monkeypatch):
    """Un caller puede sustituir el configurador (mismo punto de extensión)."""
    captured: dict = {}

    def custom(conn):
        return None

    def fake_pool_class(**kwargs):
        captured.update(kwargs)
        return _FakePool(kwargs.get("conninfo"))

    monkeypatch.setattr(pg_pool_mod.psycopg_pool, "ConnectionPool", fake_pool_class)

    create_pg_pool(_PG_URL, open=False, configure=custom)

    assert captured["configure"] is custom


# ============================================================
# FASE 5/6 — TEARDOWN DEL POOL
# ============================================================


def test_teardown_cierra_el_pool_y_limpia_la_app(pg_app):
    """El teardown cierra el pool y lo retira de la app (no queda pool muerto)."""
    fake = _init_fake_pool(pg_app)

    shutdown = register_pg_pool_teardown(pg_app)
    try:
        assert fake.closed is False

        shutdown()

        assert fake.closed is True
        assert fake.close_timeouts == [5.0]
        assert "pg_pool" not in pg_app.extensions
        assert get_pg_pool(pg_app) is None
        assert pg_pool_mod._global_pool is None
    finally:
        atexit.unregister(shutdown)


def test_teardown_es_idempotente(pg_app):
    """Ejecutar el teardown dos veces no rompe ni intenta cerrar de nuevo."""
    fake = _init_fake_pool(pg_app)

    shutdown = register_pg_pool_teardown(pg_app)
    try:
        shutdown()
        shutdown()
        shutdown()

        assert fake.closed is True
        assert fake.close_timeouts == [5.0]
    finally:
        atexit.unregister(shutdown)


def test_teardown_no_cierra_el_pool_de_otra_app(pg_app):
    """El teardown de una app jamás toca el pool de otra instancia."""
    otra = Flask("pg_otra")
    otra.config["DATABASE_URL"] = _PG_URL
    pool_otra = _init_fake_pool(otra)
    fake_esta = _init_fake_pool(pg_app)
    assert pool_otra is not fake_esta

    shutdown = register_pg_pool_teardown(pg_app)
    try:
        shutdown()

        assert fake_esta.closed is True
        assert pool_otra.closed is False
        assert get_pg_pool(otra) is pool_otra
    finally:
        atexit.unregister(shutdown)
        close_pg_pool(app=otra)


def test_teardown_queda_registrado_en_atexit(pg_app, monkeypatch):
    """El cierre se engancha al hook de fin de proceso (waitress + systemd/SIGTERM)."""
    registrados: list = []
    monkeypatch.setattr(atexit, "register", registrados.append)

    shutdown = register_pg_pool_teardown(pg_app)

    assert registrados == [shutdown]

    # El callable registrado es el que cierra el pool, no un simple registro.
    fake = _init_fake_pool(pg_app)
    registrados[0]()
    assert fake.closed is True


def test_teardown_no_se_engancha_a_teardown_appcontext(pg_app):
    """No se usa teardown_appcontext: cerraría el pool al final de cada request."""
    assert not pg_app.teardown_appcontext_funcs


# ============================================================
# FASE 5/6 — create_app() CONECTA INIT Y TEARDOWN
# ============================================================

# Guardas de create_app()/build_config() que dependen del entorno: se aíslan para
# que un FLASK_ENV=production filtrado por otro test no dispare sus RuntimeError.
_ENV_KEYS = (
    "FLASK_ENV",
    "SECRET_KEY",
    "ADMIN_PASSWORD",
    "ADMIN_PASSWORD_HASH",
    "COOKIE_SECURE",
    "TRUSTED_PROXY_COUNT",
    "SESSION_LIFETIME_SECONDS",
)


def _isolate_env(monkeypatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_create_app(monkeypatch, tmp_path):
    """Prepara create_app() con backend PostgreSQL sin tocar la red ni SQLite."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", tmp_path / "appointments.db")
    importlib.import_module("app")  # create_app() requiere la fachada importada

    pools: list[_FakePool] = []
    monkeypatch.setattr(
        pg_pool_mod,
        "create_pg_pool",
        lambda conninfo, **kw: pools.append(_FakePool(conninfo, **kw)) or pools[-1],
    )
    monkeypatch.setattr(dbmod, "init_database", lambda: None)

    registrados: list = []
    monkeypatch.setattr(atexit, "register", registrados.append)
    monkeypatch.setenv("DATABASE_URL", _PG_URL)
    return pools, registrados


def test_create_app_registra_pool_y_teardown(fake_create_app):
    """create_app() deja el pool en extensions y su teardown registrado."""
    pools, registrados = fake_create_app
    from application import create_app

    app = create_app()

    assert app.config["DB_BACKEND"] == "postgresql"
    assert len(pools) == 1
    assert app.extensions["pg_pool"] is pools[0]
    assert get_pg_pool(app) is pools[0]

    # El teardown registrado por create_app() cierra ese mismo pool.
    assert len(registrados) == 1
    registrados[0]()
    assert pools[0].closed is True
    assert "pg_pool" not in app.extensions


def test_create_app_con_sqlite_no_registra_teardown(monkeypatch, tmp_path):
    """SQLite no abre pool ni registra ningún teardown."""
    _isolate_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr(dbmod, "DATABASE_PATH", tmp_path / "appointments.db")
    monkeypatch.setattr(dbmod, "init_database", lambda: None)
    registrados: list = []
    monkeypatch.setattr(atexit, "register", registrados.append)

    importlib.import_module("app")
    from application import create_app

    app = create_app()

    assert app.config["DB_BACKEND"] == "sqlite"
    assert "pg_pool" not in app.extensions
    assert get_pg_pool(app) is None
    assert registrados == []
