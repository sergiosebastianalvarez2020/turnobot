import sqlite3
from pathlib import Path

import pytest

import database.business_domains as domain_repository
from application.business_domains import InvalidHostname, normalize_hostname

pytestmark = pytest.mark.no_pg


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
