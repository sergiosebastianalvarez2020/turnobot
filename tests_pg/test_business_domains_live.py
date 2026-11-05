import sqlite3

import psycopg
import pytest

import database.business_domains as domain_repository
from database.business_domains import (
    create_business_domain,
    get_business_domain,
    get_primary_business_domain,
    resolve_business_domain,
    revoke_business_domain,
    set_business_domain_active,
    set_business_domain_tls_status,
    set_primary_business_domain,
    verify_business_domain,
)
from tests_pg._helpers import make_pg_proxy


def test_domain_lifecycle_and_constraints(pg_pool, pg_seed, monkeypatch):
    monkeypatch.setattr(domain_repository, "get_connection", lambda: make_pg_proxy(pg_pool))
    business_id = pg_seed["business_id"]
    hostname = f"shop-{business_id}.example.com"
    domain_id = create_business_domain(business_id, hostname, "hash-token")
    domain = get_business_domain(business_id, domain_id)
    assert domain["verification_status"] == "pending"

    with pytest.raises(sqlite3.IntegrityError):
        create_business_domain(business_id, hostname.upper(), "another-token")

    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(pg_seed["url"]) as connection:
            connection.execute(
                "UPDATE business_domains SET is_active = TRUE WHERE id = %s", (domain_id,)
            )

    assert verify_business_domain(business_id, domain_id, "hash-token")
    assert set_business_domain_tls_status(business_id, domain_id, "verified")
    assert set_business_domain_active(business_id, domain_id, True)
    assert set_primary_business_domain(business_id, domain_id)
    assert resolve_business_domain(hostname)["id"] == business_id
    assert get_primary_business_domain(business_id) == hostname

    second_hostname = f"other-{business_id}.example.com"
    second_domain_id = create_business_domain(business_id, second_hostname, "second-token")
    assert verify_business_domain(business_id, second_domain_id, "second-token")
    assert set_business_domain_tls_status(business_id, second_domain_id, "verified")
    assert set_business_domain_active(business_id, second_domain_id, True)

    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(pg_seed["url"]) as connection:
            connection.execute(
                "UPDATE business_domains SET is_primary = TRUE WHERE id = %s", (second_domain_id,)
            )

    assert set_primary_business_domain(business_id, second_domain_id)
    assert get_primary_business_domain(business_id) == second_hostname

    assert set_business_domain_active(business_id, domain_id, False)
    assert resolve_business_domain(hostname) is None
    assert set_business_domain_active(business_id, domain_id, True)
    assert revoke_business_domain(business_id, domain_id)
    assert resolve_business_domain(hostname) is None
