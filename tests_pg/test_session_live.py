"""Pruebas LIVE del hardening de sesión y lifecycle del pool (PostgreSQL real).

Verifica contra el servidor, no con dobles, lo que el hardening promete:

1. `create_app()` → `init_pg_pool()` → `get_connection()` entrega una conexión
   válida y perteneciente al pool registrado en `app.extensions["pg_pool"]`.
2. La sesión de CADA conexión del pool tiene `timezone = UTC`.
3. `lock_timeout` está configurado y acota la espera por advisory locks.
4. El teardown cierra el pool, es idempotente y la app sigue sirviendo.

Sin ``TURNOBOT_PG_URL`` toda la suite hace SKIP limpio (ver conftest.py).
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
    get_pg_pool,
    init_pg_pool,
    register_pg_pool_teardown,
)
from tests_pg._helpers import conninfo_to_url, make_pg_proxy

pytestmark = pytest.mark.pg_live


def _db_url(pg_test_database: str) -> str:
    """URL postgresql:// de la base descartable (init_pg_pool detecta por prefijo)."""
    return conninfo_to_url(pg_test_database)


def _show(proxy, setting: str) -> str:
    return proxy.execute(f"SHOW {setting}").fetchone()[0]


_FACTORY_ENV_KEYS = (
    "FLASK_ENV",
    "SECRET_KEY",
    "ADMIN_PASSWORD",
    "ADMIN_PASSWORD_HASH",
    "COOKIE_SECURE",
    "TRUSTED_PROXY_COUNT",
    "SESSION_LIFETIME_SECONDS",
)


def _import_app_facade(monkeypatch) -> None:
    """Importa la fachada `app` con un entorno neutro.

    `create_app()` debe invocarse después de que la fachada esté importada (las
    rutas la referencian a nivel de módulo) y la fachada corre `load_dotenv()` al
    importarse, así que se aíslan las guardas de configuración para que un `.env`
    local con FLASK_ENV=production no altere el escenario.
    """
    for key in _FACTORY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    importlib.import_module("app")


def _backend_pid(proxy) -> int:
    return proxy.execute("SELECT pg_backend_pid() AS pid").fetchone()[0]


def _pool_pids(pool) -> set[int]:
    """PIDs de las conexiones que el pool tiene abiertas ahora mismo."""
    return {connection.info.backend_pid for connection in list(pool._pool)}


# ============================================================
# FASE 1/2 — create_app() → init_pg_pool() → get_connection()
# ============================================================


def test_create_app_entrega_conexion_del_pool_registrado(pg_test_database, monkeypatch):
    """create_app() con DATABASE_URL PostgreSQL inicializa el pool y get_connection()
    devuelve una conexión real Y perteneciente a ese pool."""
    db_url = _db_url(pg_test_database)
    monkeypatch.setattr(dbmod, "init_database", lambda: None)
    _import_app_facade(monkeypatch)

    monkeypatch.setenv("DATABASE_URL", db_url)
    from application import create_app

    app = create_app()
    try:
        assert app.config["DB_BACKEND"] == "postgresql"
        pool = app.extensions["pg_pool"]
        assert pool is not None
        assert pool.closed is False

        with app.app_context():
            # El pool se resuelve desde el contexto de aplicación...
            assert get_pg_pool() is pool

            connection = dbmod.get_connection()
            assert isinstance(connection, PgConnectionProxy)
            # ...y la conexión sale de ESE pool, no de otro.
            assert connection._pool is pool
            assert connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
            pid = _backend_pid(connection)
            # El checkout lo sirvió este pool (contador público de requests).
            assert pool.get_stats()["requests_num"] >= 1

            # La sesión naciente ya viene en UTC.
            assert _show(connection, "timezone") == PG_SESSION_TIMEZONE

            connection.close()
            # Devuelta al pool: la conexión sigue siendo suya.
            assert pid in _pool_pids(pool)
    finally:
        close_pg_pool(app=app)


def test_get_connection_fuera_de_app_context_usa_el_pool_de_create_app(
    pg_test_database, monkeypatch
):
    """Regresión del bug app.extensions/_global_pool: fuera de todo contexto Flask,
    get_connection() encuentra el pool creado por create_app() (antes: RuntimeError)."""
    db_url = _db_url(pg_test_database)
    monkeypatch.setattr(dbmod, "init_database", lambda: None)
    _import_app_facade(monkeypatch)

    monkeypatch.setenv("DATABASE_URL", db_url)
    from application import create_app

    app = create_app()
    try:
        pool = app.extensions["pg_pool"]
        connection = dbmod.get_connection()
        try:
            assert connection._pool is pool
            pid = _backend_pid(connection)
            connection.close()
            assert pid in _pool_pids(pool)
        finally:
            connection.close()
    finally:
        close_pg_pool(app=app)


# ============================================================
# FASE 3/4 — TIMEZONE DE SESIÓN
# ============================================================


def test_timezone_de_la_sesion_es_utc(pg_pool):
    assert _show(make_pg_proxy(pg_pool), "timezone") == "UTC"


def test_timezone_utc_en_cada_conexion_nueva_del_pool(pg_pool):
    """Dos conexiones simultáneas son backends distintos y ambas en UTC."""
    proxy_a = make_pg_proxy(pg_pool)
    proxy_b = make_pg_proxy(pg_pool)
    try:
        assert _backend_pid(proxy_a) != _backend_pid(proxy_b)
        assert _show(proxy_a, "timezone") == PG_SESSION_TIMEZONE
        assert _show(proxy_b, "timezone") == PG_SESSION_TIMEZONE
    finally:
        proxy_b.close()
        proxy_a.close()


def test_timezone_persiste_tras_devolver_y_reobtener_conexion(pg_pool):
    """checkout → putconn → checkout: la sesión sigue en UTC (no se pierde el set_config)."""
    primero = make_pg_proxy(pg_pool)
    pid = _backend_pid(primero)
    assert _show(primero, "timezone") == PG_SESSION_TIMEZONE
    primero.close()

    segundo = make_pg_proxy(pg_pool)
    try:
        assert _show(segundo, "timezone") == PG_SESSION_TIMEZONE
        # La conexión reutilizada del pool es la misma; forzamos además una nueva.
        assert _backend_pid(segundo) == pid
    finally:
        segundo.close()

    for _ in range(3):
        extra = make_pg_proxy(pg_pool)
        try:
            assert _show(extra, "timezone") == PG_SESSION_TIMEZONE
        finally:
            extra.close()


# ============================================================
# FASE 7/8 — LOCK TIMEOUT
# ============================================================


def test_lock_timeout_configurado_en_la_sesion(pg_pool):
    assert _show(make_pg_proxy(pg_pool), "lock_timeout") == "5s"
    assert _show(make_pg_proxy(pg_pool), "lock_timeout") == PG_LOCK_TIMEOUT


def test_lock_timeout_persiste_entre_conexiones_del_pool(pg_pool):
    primero = make_pg_proxy(pg_pool)
    assert _show(primero, "lock_timeout") == PG_LOCK_TIMEOUT
    primero.close()

    segundo = make_pg_proxy(pg_pool)
    try:
        assert _show(segundo, "lock_timeout") == PG_LOCK_TIMEOUT
    finally:
        segundo.close()


def test_lock_timeout_corta_la_espera_por_advisory_lock(pg_pool):
    """Comportamiento real: la espera por un lock ajeno se corta y se traduce al
    contrato de errores de la aplicación (sqlite3.OperationalError).

    Se baja el lock_timeout de la sesión a 300ms para que el test sea rápido y
    determinista; el valor por defecto del pool ya está verificado arriba.
    """
    locker = make_pg_proxy(pg_pool)
    esperante = make_pg_proxy(pg_pool)
    try:
        locker.execute("SELECT pg_advisory_xact_lock(4242, 1)")

        esperante.execute("SET lock_timeout = '300ms'")
        esperante.commit()

        with pytest.raises(sqlite3.OperationalError, match="lock timeout"):
            esperante.execute("SELECT pg_advisory_xact_lock(4242, 1)")
        esperante.rollback()
    finally:
        esperante.close()
        locker.close()


# ============================================================
# FASE 5/6 — TEARDOWN DEL POOL
# ============================================================


def test_teardown_cierra_el_pool_y_no_deja_conexiones(pg_test_database):
    """El teardown cierra el pool, limpia la app y una segunda ejecución es inocua."""
    db_url = _db_url(pg_test_database)
    app = Flask("pg_teardown")
    app.config["DATABASE_URL"] = db_url
    app.config["DB_BACKEND"] = "postgresql"

    pool = init_pg_pool(app)
    assert pool is not None
    pool.wait(timeout=15.0)
    shutdown = register_pg_pool_teardown(app)

    @app.route("/ping")
    def ping():
        return "pong"

    try:
        connection = make_pg_proxy(pool)
        raw_connection = connection._conn
        assert connection.execute("SELECT 1 AS ok").fetchone()[0] == 1
        connection.close()
        assert pool.closed is False
        assert get_pg_pool(app) is pool

        shutdown()

        assert pool.closed is True
        # No quedan conexiones abiertas: el pool no retiene ninguna y la conexión
        # devuelta antes del teardown quedó cerrada en el servidor.
        assert list(pool._pool) == []
        assert raw_connection.closed is True
        assert "pg_pool" not in app.extensions
        assert get_pg_pool(app) is None

        # Idempotente: repetir el teardown no lanza y la app sigue respondiendo.
        shutdown()
        shutdown()
        assert app.test_client().get("/ping").status_code == 200
    finally:
        atexit.unregister(shutdown)
        close_pg_pool(app=app)


def test_get_connection_tras_el_teardown_falla_de_forma_explicita(pg_test_database, monkeypatch):
    """Sin pool no se recicla uno cerrado: el error dice qué falta."""
    db_url = _db_url(pg_test_database)
    monkeypatch.setenv("DATABASE_URL", db_url)
    app = Flask("pg_teardown_2")
    app.config["DATABASE_URL"] = db_url
    app.config["DB_BACKEND"] = "postgresql"

    pool = init_pg_pool(app)
    assert pool is not None
    pool.wait(timeout=15.0)
    shutdown = register_pg_pool_teardown(app)
    try:
        shutdown()
        assert pool.closed is True
        with pytest.raises(RuntimeError, match="no está inicializado"):
            dbmod.get_connection()
    finally:
        atexit.unregister(shutdown)
        close_pg_pool(app=app)


def test_teardown_no_afecta_a_otra_app_con_pool(pg_test_database):
    """Dos apps con pool propio: el teardown de una no cierra el pool de la otra."""
    db_url = _db_url(pg_test_database)
    app_a = Flask("pg_a")
    app_b = Flask("pg_b")
    app_a.config["DATABASE_URL"] = db_url
    app_b.config["DATABASE_URL"] = db_url

    pool_a = init_pg_pool(app_a)
    pool_b = init_pg_pool(app_b)
    assert pool_a is not None and pool_b is not None
    assert pool_a is not pool_b
    pool_a.wait(timeout=15.0)
    pool_b.wait(timeout=15.0)

    shutdown_b = register_pg_pool_teardown(app_b)
    try:
        shutdown_b()

        assert pool_b.closed is True
        assert pool_a.closed is False
        with app_a.app_context():
            connection = make_pg_proxy(get_pg_pool())
            try:
                assert connection.execute("SELECT 1 AS ok").fetchone()[0] == 1
            finally:
                connection.close()
    finally:
        atexit.unregister(shutdown_b)
        close_pg_pool(app=app_b)
        close_pg_pool(app=app_a)


def test_sesion_configurada_tambien_en_pools_creados_por_init_pg_pool(pg_test_database):
    """La configuración de sesión no depende de quién crea el pool."""
    db_url = _db_url(pg_test_database)
    app = Flask("pg_session")
    app.config["DATABASE_URL"] = db_url
    pool = init_pg_pool(app, min_size=1, max_size=3)
    assert pool is not None
    pool.wait(timeout=15.0)
    try:
        with app.app_context():
            proxy = make_pg_proxy(get_pg_pool())
            try:
                assert proxy._pool is pool
                assert _show(proxy, "timezone") == PG_SESSION_TIMEZONE
                assert _show(proxy, "lock_timeout") == PG_LOCK_TIMEOUT
            finally:
                proxy.close()
    finally:
        close_pg_pool(app=app)
        assert pg_pool_mod._global_pool is None
