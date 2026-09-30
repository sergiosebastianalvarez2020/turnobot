"""Regresión: los scripts CLI standalone deben administrar el pool PostgreSQL.

Contexto del bug: tras el cutover a PostgreSQL, `database.database.get_connection()`
delega en el pool cuando `DATABASE_URL` es postgresql. Ese pool solo se crea en
`create_app()`, es decir bajo contexto Flask. Los scripts CLI corren SIN contexto
Flask, así que sin un `init_pg_pool()` explícito `get_pg_pool()` devolvía `None` y
la operación moría con:

    RuntimeError: El pool PostgreSQL no está inicializado. create_app() debe
    invocar init_pg_pool() antes de get_connection().

Estos tests fijan el contrato de los workers CLI: inicializan el pool ANTES de
tocar la base, lo usan a través de `get_connection()` (igual que producción) y lo
cierran al terminar, incluso si la operación explota.

El pool real se sustituye por un doble: lo que se verifica es el ORDEN y el
ciclo de vida, no la conectividad a un PostgreSQL de verdad.
"""


import pytest

from database import database as database_mod
from database import pg_pool

PG_URL = "postgresql://turnobot:secret@localhost:5432/turnobot"


class _FakeConn:
    """Conexión mínima; `autocommit=True` evita el rollback en PgConnectionProxy.close()."""

    autocommit = True


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()
        self.closed = False
        self.putconn_calls = 0

    def getconn(self):
        return self.conn

    def putconn(self, conn=None):
        self.putconn_calls += 1

    def close(self, timeout=None):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_global_pool():
    """`_global_pool` es estado de módulo: se restaura para no filtrar entre tests."""
    previous = pg_pool._global_pool
    yield
    pg_pool._global_pool = previous


@pytest.fixture
def pool_spy(monkeypatch):
    """Devuelve el doble del pool y los registros de create_pg_pool()."""
    pool = _FakePool()
    created = {}

    def fake_create_pg_pool(conninfo, **kwargs):
        created["conninfo"] = conninfo
        created["called"] = True
        return pool

    monkeypatch.setattr(pg_pool, "create_pg_pool", fake_create_pg_pool)
    pool.created = created
    return pool


def test_retry_initializa_pool_antes_de_la_db_y_lo_cierra(monkeypatch, pool_spy):
    """Regresión del fallo real: durante la operación el pool debe existir."""
    import scripts.retry_failed_notifications as retry_runner

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(retry_runner, "smtp_configured", lambda: True)

    seen = {}

    def spy_list_failed(business_id=None, limit=100):
        # Esto es exactamente lo que explotaba en producción.
        seen["pool"] = pg_pool.get_pg_pool()
        seen["connection"] = database_mod.get_connection()
        seen["limit"] = limit
        return []

    monkeypatch.setattr(retry_runner, "list_failed_notifications_scoped", spy_list_failed)

    retry_runner._run_once(business_id=1, limit=7)

    assert pool_spy.created["called"] is True
    assert pool_spy.created["conninfo"] == PG_URL
    assert seen["pool"] is pool_spy
    assert seen["connection"] is not None
    assert seen["limit"] == 7
    assert pool_spy.closed is True
    assert pg_pool._global_pool is None


def test_send_reminders_inicializa_pool_antes_de_la_db_y_lo_cierra(monkeypatch, pool_spy):
    """El mismo contrato para el worker diario, que aún no había fallado."""
    import scripts.send_reminders as reminder_runner

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(reminder_runner, "smtp_configured", lambda: True)

    seen = {}

    def spy_list_businesses():
        seen["pool"] = pg_pool.get_pg_pool()
        seen["connection"] = database_mod.get_connection()
        return []

    monkeypatch.setattr(reminder_runner, "list_all_businesses_scoped", spy_list_businesses)

    assert reminder_runner._run_once() == 0

    assert pool_spy.created["called"] is True
    assert pool_spy.created["conninfo"] == PG_URL
    assert seen["pool"] is pool_spy
    assert seen["connection"] is not None
    assert pool_spy.closed is True
    assert pg_pool._global_pool is None


@pytest.mark.parametrize("module_name", ["retry_failed_notifications", "send_reminders"])
def test_pool_se_cierra_aunque_la_operacion_falle(monkeypatch, pool_spy, module_name):
    """Una excepción en la operación no debe dejar el pool abierto."""
    runner = __import__(f"scripts.{module_name}", fromlist=["_run_once"])

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(runner, "smtp_configured", lambda: True)

    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db explotó"))  # noqa: E731

    if module_name == "retry_failed_notifications":
        monkeypatch.setattr(runner, "list_failed_notifications_scoped", boom)
        call = lambda: runner._run_once()  # noqa: E731
    else:
        monkeypatch.setattr(runner, "list_all_businesses_scoped", boom)
        call = lambda: runner._run_once()  # noqa: E731

    with pytest.raises(RuntimeError, match="db explotó"):
        call()

    assert pool_spy.closed is True
    assert pg_pool._global_pool is None


def test_normaliza_postgres_url_legacy(monkeypatch, pool_spy):
    """`postgres://` debe llegar normalizada a `postgresql://` (libpq/psycopg3)."""
    import scripts.send_reminders as reminder_runner

    monkeypatch.setenv("DATABASE_URL", "postgres://turnobot:secret@localhost:5432/turnobot")
    monkeypatch.setattr(reminder_runner, "smtp_configured", lambda: True)
    monkeypatch.setattr(reminder_runner, "list_all_businesses_scoped", lambda: [])

    reminder_runner._run_once()

    assert pool_spy.created["conninfo"] == PG_URL


def test_sin_database_url_no_se_crea_pool(monkeypatch, pool_spy):
    """Backend SQLite: no hay nada que inicializar y el worker sigue operando."""
    import scripts.send_reminders as reminder_runner

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(reminder_runner, "smtp_configured", lambda: True)
    monkeypatch.setattr(reminder_runner, "list_all_businesses_scoped", lambda: [])

    assert reminder_runner._run_once() == 0
    assert "called" not in pool_spy.created
    assert pool_spy.closed is False


def test_falla_ruidosamente_si_postgres_no_puede_inicializar(monkeypatch):
    """Un pool ausente con backend postgresql debe ser un error explícito.

    Antes esto se manifestaba tarde y como `RuntimeError` de `get_connection()`,
    recién después de arrancar el worker.
    """

    def boom(conninfo, **kwargs):
        raise RuntimeError("no se pudo crear el pool")

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(pg_pool, "create_pg_pool", boom)

    import scripts.send_reminders as reminder_runner

    with pytest.raises(RuntimeError, match="no se pudo crear el pool"):
        reminder_runner._run_once()


def test_el_bug_original_sigue_siendo_reproducible_sin_el_fix(monkeypatch):
    """Guarda de que los tests anteriores no son decorativos.

    Sin `init_pg_pool()` el proceso CLI sigue sin pool y `get_connection()`
    vuelve a levantar el error original. Si este test dejara de fallar, el
    contrato previo estaría probando otra cosa.
    """
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(pg_pool, "_global_pool", None)

    assert pg_pool.get_pg_pool() is None

    with pytest.raises(RuntimeError, match="no está inicializado"):
        database_mod.get_connection()


def test_worker_cli_no_necesita_contexto_flask(monkeypatch, pool_spy):
    """Los workers CLI corren sin contexto de aplicación Flask."""
    from flask import current_app

    import scripts.retry_failed_notifications as retry_runner

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(retry_runner, "smtp_configured", lambda: True)

    checked = {}

    def spy_list_failed(business_id=None, limit=100):
        checked["in_app_context"] = current_app
        return []

    monkeypatch.setattr(retry_runner, "list_failed_notifications_scoped", spy_list_failed)

    retry_runner._run_once()

    with pytest.raises(RuntimeError):
        current_app._get_current_object()  # sigue fuera de contexto al terminar


def test_smoke_get_connection_devuelve_proxy_del_pool(monkeypatch, pool_spy):
    """`get_connection()` debe entregar el proxy del pool, no una conexión nueva."""
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    assert pg_pool.init_pg_pool(conninfo=PG_URL) is pool_spy

    connection = database_mod.get_connection()

    assert isinstance(connection, pg_pool.PgConnectionProxy)
    assert connection._conn is pool_spy.conn
