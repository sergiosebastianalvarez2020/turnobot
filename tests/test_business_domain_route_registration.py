import pytest
from flask import Flask, g

from routes import business_domains as business_domain_routes
from routes import register_blueprints

pytestmark = pytest.mark.no_pg


EXPECTED_RULES = {
    ("/admin/dominios", "business_domains"): {"GET"},
    ("/b/<slug>/admin/dominios", "business_domains_slug"): {"GET"},
    ("/admin/dominios/agregar", "add_business_domain"): {"POST"},
    ("/b/<slug>/admin/dominios/agregar", "add_business_domain_slug"): {"POST"},
    ("/admin/dominios/<int:domain_id>/verificar", "business_domain_verificar"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/verificar", "business_domain_verificar_slug"): {
        "POST"
    },
    ("/admin/dominios/<int:domain_id>/tls", "business_domain_tls"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/tls", "business_domain_tls_slug"): {"POST"},
    ("/admin/dominios/<int:domain_id>/activar", "business_domain_activar"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/activar", "business_domain_activar_slug"): {"POST"},
    ("/admin/dominios/<int:domain_id>/desactivar", "business_domain_desactivar"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/desactivar", "business_domain_desactivar_slug"): {
        "POST"
    },
    ("/admin/dominios/<int:domain_id>/revocar", "business_domain_revocar"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/revocar", "business_domain_revocar_slug"): {"POST"},
    ("/admin/dominios/<int:domain_id>/principal", "business_domain_principal"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/principal", "business_domain_principal_slug"): {
        "POST"
    },
    ("/admin/dominios/<int:domain_id>/rotar-token", "business_domain_rotar_token"): {"POST"},
    ("/b/<slug>/admin/dominios/<int:domain_id>/rotar-token", "business_domain_rotar_token_slug"): {
        "POST"
    },
}


def _domain_rules(app):
    return {
        (rule.rule, rule.endpoint): rule.methods - {"HEAD", "OPTIONS"}
        for rule in app.url_map.iter_rules()
        if "dominios" in rule.rule
    }


def test_application_route_registry_includes_all_business_domain_rules():
    app = Flask(__name__)
    register_blueprints(app)

    domain_rules = [rule for rule in app.url_map.iter_rules() if "dominios" in rule.rule]
    assert len(domain_rules) == 18
    assert _domain_rules(app) == EXPECTED_RULES


def test_domain_management_rejects_unauthorized_users(monkeypatch):
    app = Flask(__name__)
    business_domain_routes.register(app)
    app.before_request(lambda: setattr(g, "current_business", {"id": 2, "slug": "shop"}))
    monkeypatch.setattr(
        business_domain_routes, "_require_admin_membership", lambda: ("Unauthorized", 401)
    )

    client = app.test_client()
    assert client.get("/admin/dominios").status_code == 401
    assert client.get("/b/shop/admin/dominios").status_code == 401
    assert client.post("/admin/dominios/1/activar").status_code == 401
    assert client.post("/b/shop/admin/dominios/1/activar").status_code == 401
