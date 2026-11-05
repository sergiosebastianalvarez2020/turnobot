import psycopg

from database.business_domains import (
    create_business_domain,
    get_business_domain,
    set_business_domain_active,
    set_business_domain_tls_status,
    verify_business_domain,
)
from tests._pg_compat import PostgreSQLTestCase
from tests_pg._helpers import seed_business, seed_business_settings


class TestBusinessDomainRoutes(PostgreSQLTestCase):
    def _create_active_domain(self):
        business_id = seed_business(self.pg_test_url)
        seed_business_settings(self.pg_test_url, business_id)
        hostname = f"shop-{business_id}.example.com"
        with psycopg.connect(self.pg_test_url) as connection:
            slug = connection.execute(
                "SELECT slug FROM businesses WHERE id = %s", (business_id,)
            ).fetchone()[0]
        with self.app.app_context():
            domain_id = create_business_domain(business_id, hostname, "hash-token")
            domain = get_business_domain(business_id, domain_id)
            assert verify_business_domain(business_id, domain_id, domain["verification_token_hash"])
            assert set_business_domain_tls_status(business_id, domain_id, "verified")
            assert set_business_domain_active(business_id, domain_id, True)
        return business_id, slug, hostname

    def test_custom_domain_root_and_slug_are_bound_to_the_resolved_tenant(self):
        _, slug, hostname = self._create_active_domain()
        client = self.app.test_client()

        response = client.get("/", base_url=f"https://{hostname}")
        assert response.status_code == 200
        assert slug in response.get_data(as_text=True)

        response = client.get("/b/not-the-business", base_url=f"https://{hostname}")
        assert response.status_code == 404

    def test_unknown_and_insecure_hosts_never_resolve_business_one(self):
        _, _, hostname = self._create_active_domain()
        client = self.app.test_client()

        unknown = client.get("/", base_url="https://unknown.example.com")
        insecure = client.get("/", base_url=f"http://{hostname}")
        platform = client.get("/", base_url="http://localhost")

        assert unknown.status_code == 404
        assert insecure.status_code == 404
        assert platform.status_code == 200
        assert b"marketing-theme" in platform.data
        assert b"El Corte" not in unknown.data

    def test_inactive_business_does_not_resolve_on_its_custom_domain(self):
        business_id, _, hostname = self._create_active_domain()
        with psycopg.connect(self.pg_test_url) as connection:
            connection.execute("UPDATE businesses SET active = FALSE WHERE id = %s", (business_id,))

        response = self.app.test_client().get("/", base_url=f"https://{hostname}")
        assert response.status_code == 404

    def test_forwarded_host_does_not_override_platform_host_without_trusted_proxy(self):
        response = self.app.test_client().get(
            "/", headers={"X-Forwarded-Host": "unknown.example.com"}, base_url="http://localhost"
        )
        assert response.status_code == 200
        assert b"marketing-theme" in response.data
