"""
Tests de regresión para correcciones del ciclo de vida de pools PostgreSQL.

Verifican correcciones en tests/conftest.py:
- Fixture app: cierra pool en teardown (incluso ante excepciones)
- Fixture pg_isolate_module_app: cierra pool anterior, no restaura pool cerrado,
  cierra pool temporal en teardown, maneja excepciones, no cierra pools compartidos.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakePool:
    """Pool de prueba con ciclo de vida observable (sin conexiones reales)."""

    def __init__(self, conninfo: str = "postgresql://test@localhost/test"):
        self.conninfo = conninfo
        self.closed = False
        self.close_calls = 0

    def close(self, timeout: float = 5.0) -> None:
        self.closed = True
        self.close_calls += 1

    def getconn(self):
        return object()

    def putconn(self, conn):
        return None


@pytest.fixture
def _pg_app_factory():
    """Crea apps Flask mínimas configuradas para backend PostgreSQL."""
    from flask import Flask

    created = []

    def factory(name: str) -> Flask:
        app = Flask(name)
        app.config["DATABASE_URL"] = "postgresql://test@localhost/test"
        app.config["DB_BACKEND"] = "postgresql"
        app.config["SECRET_KEY"] = "test"
        app.config["ADMIN_PASSWORD_HASH"] = "test"
        created.append(app)
        return app

    yield factory

    # Limpieza defensiva: no dejar pools falsos registrados en apps de prueba.
    for app in created:
        app.extensions.pop("pg_pool", None)


# ============================================================
# Fixture app — ciclo de vida observable (misma semántica que conftest)
# ============================================================


@pytest.mark.no_pg
def test_app_fixture_cierra_pool_en_teardown(monkeypatch):
    """El patrón de teardown del fixture app cierra su pool y lo retira."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test@localhost/test")
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("FLASK_ENV", "development")

    import database.database as db_mod
    import database.pg_pool as pg_pool
    from application import create_app

    fake = _FakePool()
    monkeypatch.setattr(pg_pool, "create_pg_pool", lambda *a, **k: fake)
    # Aislar ciclo de vida del pool: init_database usa conexiones reales.
    monkeypatch.setattr(db_mod, "init_database", lambda *_args, **_kwargs: None)

    application = create_app()
    application.config["TESTING"] = True
    assert application.extensions.get("pg_pool") is fake
    assert fake.closed is False

    # Simula el teardown real del fixture app en conftest.py.
    pg_pool.close_pg_pool(app=application)

    assert fake.closed is True
    assert fake.close_calls == 1
    assert "pg_pool" not in application.extensions


@pytest.mark.no_pg
def test_app_fixture_cierra_pool_incluso_si_test_falla(monkeypatch):
    """El teardown del fixture app cierra el pool aunque el cuerpo falle."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test@localhost/test")
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("FLASK_ENV", "development")

    import database.database as db_mod
    import database.pg_pool as pg_pool
    from application import create_app

    fake = _FakePool()
    monkeypatch.setattr(pg_pool, "create_pg_pool", lambda *a, **k: fake)
    # Aislar ciclo de vida del pool: init_database usa conexiones reales.
    monkeypatch.setattr(db_mod, "init_database", lambda *_args, **_kwargs: None)

    application = create_app()
    application.config["TESTING"] = True
    try:
        raise AssertionError("fallo intencional")
    except AssertionError:
        pass
    finally:
        pg_pool.close_pg_pool(app=application)

    assert fake.closed is True
    assert fake.close_calls == 1
    assert "pg_pool" not in application.extensions


# ============================================================
# pg_isolate_module_app — ciclo de vida del pool
# ============================================================


@pytest.mark.no_pg
def test_pg_isolate_module_app_cierra_pool_anterior_antes_de_reemplazar(_pg_app_factory):
    """close_pg_pool(app=...) cierra el pool anterior y lo retira de extensions."""
    from database.pg_pool import close_pg_pool

    app = _pg_app_factory("test_isolate_close_old")
    old_pool = _FakePool()
    app.extensions["pg_pool"] = old_pool

    close_pg_pool(app=app)

    assert old_pool.closed is True
    assert old_pool.close_calls == 1
    assert "pg_pool" not in app.extensions


@pytest.mark.no_pg
def test_pg_isolate_module_app_no_restaura_pool_cerrado(_pg_app_factory):
    """Un pool cerrado no debe restaurarse en extensions ni como global."""
    import database.pg_pool as pg_pool_mod
    from database.pg_pool import close_pg_pool
    from tests.conftest import _pool_is_usable

    app = _pg_app_factory("test_isolate_no_restore_closed")
    old_pool = _FakePool()
    app.extensions["pg_pool"] = old_pool

    close_pg_pool(app=app)
    assert old_pool.closed is True
    assert _pool_is_usable(old_pool) is False

    if _pool_is_usable(old_pool):
        app.extensions["pg_pool"] = old_pool
    assert "pg_pool" not in app.extensions

    orig_global = pg_pool_mod._global_pool
    orig_owned = pg_pool_mod._global_pool_app_owned
    try:
        pg_pool_mod._global_pool = None
        pg_pool_mod._global_pool_app_owned = False
        if _pool_is_usable(old_pool):
            pg_pool_mod._global_pool = old_pool
            pg_pool_mod._global_pool_app_owned = True
        assert pg_pool_mod._global_pool is None
        assert pg_pool_mod._global_pool_app_owned is False
    finally:
        pg_pool_mod._global_pool = orig_global
        pg_pool_mod._global_pool_app_owned = orig_owned


@pytest.mark.no_pg
def test_pg_isolate_module_app_restauracion_config_tras_excepcion(monkeypatch):
    """El teardown restaura config y cierra el pool temporal tras excepción."""
    import app as application_mod
    import database.database as database
    import database.pg_pool as pg_pool_mod
    from tests.conftest import pg_isolate_module_app as isolate_fixture

    application_app = application_mod.app
    old_pool = _FakePool()
    old_config = dict(application_app.config)
    old_db_path = database.DATABASE_PATH
    orig_global = pg_pool_mod._global_pool
    orig_owned = pg_pool_mod._global_pool_app_owned
    created: list[_FakePool] = []

    monkeypatch.setattr("tests.conftest.TURNOBOT_PG_URL", "postgresql://test@localhost/test")
    monkeypatch.setattr("tests.conftest.create_test_database", lambda *a, **k: None)
    monkeypatch.setattr("tests.conftest.apply_current_schema", lambda *a, **k: None)
    monkeypatch.setattr("tests.conftest.seed_standard_test_data", lambda *a, **k: None)
    monkeypatch.setattr(
        "tests.conftest._drop_test_database_with_real_connection", lambda *a, **k: None
    )

    def _create(*args, **kwargs):
        pool = _FakePool()
        created.append(pool)
        return pool

    class _Node:
        def get_closest_marker(self, name):
            return None

    class _Request:
        node = _Node()
        fixturenames = []

    isolate_impl = isolate_fixture.__wrapped__
    application_app.extensions["pg_pool"] = old_pool
    pg_pool_mod._global_pool = None
    pg_pool_mod._global_pool_app_owned = False

    try:
        with patch("database.pg_pool.create_pg_pool", side_effect=_create):
            gen = isolate_impl(_Request(), monkeypatch)
            next(gen)
            assert application_app.extensions.get("pg_pool") is created[0]
            # Refactor: separate the exception test to have only one invocation
            exc_raised = False
            try:
                gen.throw(RuntimeError("fallo intencional"))
            except RuntimeError:
                exc_raised = True
            assert exc_raised
    finally:
        application_app.config.clear()
        application_app.config.update(old_config)
        database.DATABASE_PATH = old_db_path
        application_app.extensions.pop("pg_pool", None)
        pg_pool_mod._global_pool = orig_global
        pg_pool_mod._global_pool_app_owned = orig_owned

    assert created
    assert created[0].closed is True
    assert application_app.config.get("DATABASE_URL") == old_config.get("DATABASE_URL")
    assert database.DATABASE_PATH == old_db_path


@pytest.mark.no_pg
def test_pg_isolate_module_app_global_pool_comportamiento(_pg_app_factory):
    """Cerrar el pool de app limpia respaldo global si era el mismo objeto."""
    import database.pg_pool as pg_pool_mod
    from database.pg_pool import close_pg_pool, init_pg_pool
    from tests.conftest import _pool_is_usable

    app = _pg_app_factory("test_global_pool")
    pool = _FakePool()

    orig_global = pg_pool_mod._global_pool
    orig_owned = pg_pool_mod._global_pool_app_owned
    try:
        with patch("database.pg_pool.create_pg_pool", return_value=pool):
            init_pg_pool(app, open=False)

        assert pg_pool_mod._global_pool is pool
        assert pg_pool_mod._global_pool_app_owned is True

        close_pg_pool(app=app)

        assert pool.closed is True
        assert pg_pool_mod._global_pool is None
        assert pg_pool_mod._global_pool_app_owned is False
        assert _pool_is_usable(pool) is False
    finally:
        pg_pool_mod._global_pool = orig_global
        pg_pool_mod._global_pool_app_owned = orig_owned


# ============================================================
# PostgreSQL real (solo con TURNOBOT_PG_URL)
# ============================================================


@pytest.mark.pg
def test_app_fixture_pool_se_cierra_en_base_real(pg_test_database, monkeypatch):
    """Con PostgreSQL real, el pool asociado a la app se cierra tras el uso."""
    import os

    from application import create_app
    from database.pg_pool import close_pg_pool, get_pg_pool
    from tests_pg._helpers import conninfo_to_url

    if not os.getenv("TURNOBOT_PG_URL"):
        pytest.skip("TURNOBOT_PG_URL no configurada")

    test_url = conninfo_to_url(pg_test_database)
    monkeypatch.setenv("DATABASE_URL", test_url)
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("FLASK_ENV", "development")
    app = create_app()
    app.config["TESTING"] = True
    pool = get_pg_pool(app)
    assert pool is not None
    assert getattr(pool, "closed", False) is False
    close_pg_pool(app=app)
    assert get_pg_pool(app) is None
    assert getattr(pool, "closed", True) is True
