import sqlite3
from pathlib import Path

import psycopg
import psycopg_pool
import pytest
from flask import Flask

import database.business_domains as domain_repository
import database.database as dbmod
import database.pg_pool as pg_pool_mod
from application.business_domains import InvalidHostname, normalize_hostname
from database.pg_pool import create_pg_pool

pytestmark = pytest.mark.no_pg


class _StubPgConnection:
    """Conexión psycopg doble: registra la consulta y devuelve filas cargadas."""

    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.queries = []
        self.autocommit = False
        self.connection = self

    def cursor(self):
        return self

    def execute(self, query, params=None):
        if self.error is not None:
            raise self.error
        self.queries.append((query, params))
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    @property
    def description(self):
        return (("id",), ("name",), ("slug",))

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _StubPool:
    """Pool doble con la misma superficie que consume get_pg_pool_connection()."""

    def __init__(self, factory=None, conninfo=None):
        self.conninfo = conninfo
        self.factory = factory
        self.checkouts = []
        self.putbacks = []

    def getconn(self):
        connection = self.factory() if self.factory else None
        self.checkouts.append(connection)
        return connection

    def putconn(self, connection):
        self.putbacks.append(connection)


class _PoisonPool:
    """Pool residual que falla si alguna ruta llega a utilizarlo."""

    def __init__(self, conninfo=None):
        self.conninfo = conninfo
        self.getconn_calls = 0

    def getconn(self):
        self.getconn_calls += 1
        raise AssertionError("el pool PostgreSQL residual no debe utilizarse")

    def putconn(self, connection):  # pragma: no cover - nunca se alcanza
        raise AssertionError("el pool PostgreSQL residual no debe utilizarse")


def _pg_app(name):
    app = Flask(name)
    app.config["DB_BACKEND"] = "postgresql"
    app.config["DATABASE_URL"] = "postgresql://usr:pwd@localhost:5432/turnobot_test"
    return app


def _sqlite_connection_factory(database_path):
    def connect():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    return connect


def _initialize_domain_schema(database_path):
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE businesses (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO businesses (id, name, slug, active)
            VALUES (1, 'Negocio de prueba', 'negocio-prueba', 1);
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_email TEXT NOT NULL DEFAULT '',
                business_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        migration = Path(__file__).resolve().parents[1] / "migrations" / "025_business_domains.sql"
        connection.executescript(migration.read_text(encoding="utf-8"))
    finally:
        connection.close()


def test_normalize_hostname_canonicalizes_idna_and_dns_root_dot():
    assert normalize_hostname("  WWW.BARBERÍA.COM.AR. ") == "www.xn--barbera-dza.com.ar"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",
        "example.com:443",
        "192.0.2.10",
        "localhost",
        "shop.local",
        "*.example.com",
        "bad_label.example.com",
        "example..com",
        "example.c",
        "example.com..",
    ],
)
def test_normalize_hostname_rejects_non_public_or_malformed_values(value):
    with pytest.raises(InvalidHostname):
        normalize_hostname(value)


def test_domain_repository_enforces_verified_tls_and_activation(tmp_path, monkeypatch):
    database_path = tmp_path / "domains.sqlite"
    _initialize_domain_schema(database_path)
    monkeypatch.setattr(
        domain_repository, "get_connection", _sqlite_connection_factory(database_path)
    )

    domain_id = domain_repository.create_business_domain(
        1, "shop.example.com", "hash-token", "owner@example.com", "203.0.113.10"
    )
    pending = domain_repository.get_business_domain(1, domain_id)
    assert pending["verification_status"] == "pending"
    assert domain_repository.resolve_business_domain("shop.example.com") is None
    assert not domain_repository.set_business_domain_active(1, domain_id, True)

    assert domain_repository.verify_business_domain(1, domain_id, "hash-token")
    assert not domain_repository.verify_business_domain(1, domain_id, "hash-token")
    assert domain_repository.resolve_business_domain("shop.example.com") is None
    assert domain_repository.set_business_domain_tls_status(1, domain_id, "verified")
    assert domain_repository.set_business_domain_active(1, domain_id, True)
    assert domain_repository.set_primary_business_domain(1, domain_id)

    resolved = domain_repository.resolve_business_domain("shop.example.com")
    assert resolved == {"id": 1, "name": "Negocio de prueba", "slug": "negocio-prueba"}
    assert domain_repository.get_primary_business_domain(1) == "shop.example.com"

    assert domain_repository.set_business_domain_active(1, domain_id, False)
    assert domain_repository.resolve_business_domain("shop.example.com") is None
    assert domain_repository.set_business_domain_active(1, domain_id, True)
    assert domain_repository.revoke_business_domain(1, domain_id)
    assert domain_repository.resolve_business_domain("shop.example.com") is None
    assert domain_repository.get_primary_business_domain(1) is None

    connection = sqlite3.connect(database_path)
    try:
        actions = connection.execute("SELECT action FROM audit_log ORDER BY id").fetchall()
    finally:
        connection.close()
    assert [row[0] for row in actions] == [
        "business_domain.created",
        "business_domain.verified",
        "business_domain.tls_verified",
        "business_domain.activated",
        "business_domain.primary_changed",
        "business_domain.deactivated",
        "business_domain.activated",
        "business_domain.revoked",
    ]


def test_domain_repository_scopes_mutations_and_rejects_expired_tokens(tmp_path, monkeypatch):
    database_path = tmp_path / "domains.sqlite"
    _initialize_domain_schema(database_path)
    monkeypatch.setattr(
        domain_repository, "get_connection", _sqlite_connection_factory(database_path)
    )
    domain_id = domain_repository.create_business_domain(1, "shop.example.com", "hash-token")

    assert domain_repository.get_business_domain(2, domain_id) is None
    assert not domain_repository.verify_business_domain(2, domain_id, "hash-token")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE business_domains SET verification_expires_at = '2000-01-01 00:00:00' "
            "WHERE id = ?",
            (domain_id,),
        )
        connection.commit()
    finally:
        connection.close()
    assert not domain_repository.verify_business_domain(1, domain_id, "hash-token")


def test_sqlite_con_pool_postgresql_residual_no_lo_utiliza(tmp_path, monkeypatch):
    """Con backend SQLite la consulta va a SQLite aunque exista un pool PG residual."""
    database_path = tmp_path / "domains.sqlite"
    _initialize_domain_schema(database_path)
    monkeypatch.setattr(dbmod, "DATABASE_PATH", database_path)

    domain_id = domain_repository.create_business_domain(
        1, "shop.example.com", "hash-token", "owner@example.com", "203.0.113.10"
    )
    domain_repository.verify_business_domain(1, domain_id, "hash-token")
    domain_repository.set_business_domain_tls_status(1, domain_id, "verified")
    domain_repository.set_business_domain_active(1, domain_id, True)

    residual = _PoisonPool()
    monkeypatch.setattr(pg_pool_mod, "_global_pool", residual)

    app = Flask("sqlite_con_pool_residual")
    app.config["DB_BACKEND"] = "sqlite"
    app.config["DATABASE_URL"] = ""
    with app.app_context():
        resolved = domain_repository.resolve_business_domain("shop.example.com")

    assert resolved == {"id": 1, "name": "Negocio de prueba", "slug": "negocio-prueba"}
    assert residual.getconn_calls == 0, "el pool PostgreSQL residual fue utilizado"


def test_postgresql_con_pool_proprio_realiza_la_consulta():
    """El backend PostgreSQL consulta a través de SU pool registrado en la app."""
    pool = _StubPool(
        factory=lambda: _StubPgConnection(rows=[(7, "Negocio PG", "negocio-pg")]),
        conninfo="postgresql://usr:pwd@localhost:5432/turnobot_test",
    )
    app = _pg_app("pg_con_pool")
    app.extensions["pg_pool"] = pool

    with app.app_context():
        resolved = domain_repository.resolve_business_domain("shop.example.com")

    assert resolved == {"id": 7, "name": "Negocio PG", "slug": "negocio-pg"}
    assert len(pool.checkouts) == 1, "la función no usó el pool de la app"
    assert pool.checkouts[0].queries, "la consulta SQL no se ejecutó"
    assert len(pool.putbacks) == 1, "la conexión no devolvió al pool"


def test_postgresql_con_consulta_exitosa_sin_coincidencias_devuelve_none():
    """Una consulta exitosa sin filas devuelve None (el dominio no existe)."""
    pool = _StubPool(factory=lambda: _StubPgConnection(rows=[]))
    app = _pg_app("pg_sin_coincidencias")
    app.extensions["pg_pool"] = pool

    with app.app_context():
        resolved = domain_repository.resolve_business_domain("missing.example.com")

    assert resolved is None
    assert len(pool.checkouts) == 1, "la función no consultó el pool"
    assert pool.checkouts[0].queries, "la consulta SQL no se ejecutó"


def test_postgresql_sin_pool_genera_error_operativo(monkeypatch):
    """Sin pool no hay consulta posible: error operativo, jamás None."""
    monkeypatch.setattr(pg_pool_mod, "_global_pool", None)
    app = _pg_app("pg_sin_pool")
    assert "pg_pool" not in app.extensions

    with app.app_context():
        with pytest.raises(RuntimeError, match="no está inicializado"):
            domain_repository.resolve_business_domain("shop.example.com")


def test_postgresql_con_pool_cerrado_genera_error_operativo():
    """Un pool registrado pero cerrado (open=False sin abrir) es error operativo."""
    pool = create_pg_pool("postgresql://usr:pwd@localhost:5432/turnobot_test", open=False)
    app = _pg_app("pg_pool_cerrado")
    app.extensions["pg_pool"] = pool
    try:
        with app.app_context():
            with pytest.raises(psycopg.OperationalError) as excinfo:
                domain_repository.resolve_business_domain("shop.example.com")
        assert isinstance(excinfo.value, psycopg_pool.PoolClosed)
    finally:
        pool.close()


def test_postgresql_con_conexion_terminada_propaga_el_error():
    """Una conexión terminada propaga el error; no se convierte en None."""
    pool = _StubPool(
        factory=lambda: _StubPgConnection(
            error=psycopg.OperationalError("the connection is closed")
        )
    )
    app = _pg_app("pg_conexion_terminada")
    app.extensions["pg_pool"] = pool

    with app.app_context():
        with pytest.raises(sqlite3.OperationalError, match="connection is closed"):
            domain_repository.resolve_business_domain("shop.example.com")

    assert len(pool.checkouts) == 1


def test_postgresql_con_error_sql_propaga_el_error():
    """Un error SQL se propaga (traducido al contrato sqlite3) y no se oculta."""
    pool = _StubPool(
        factory=lambda: _StubPgConnection(
            error=psycopg.errors.SyntaxError('syntax error at or near "FROM"')
        )
    )
    app = _pg_app("pg_error_sql")
    app.extensions["pg_pool"] = pool

    with app.app_context():
        with pytest.raises(sqlite3.ProgrammingError, match="syntax error"):
            domain_repository.resolve_business_domain("shop.example.com")

    assert len(pool.checkouts) == 1
