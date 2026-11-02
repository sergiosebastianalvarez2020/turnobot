import pytest
from flask import Flask, g, session

import routes.admin as admin_routes
import routes.auth as auth_routes

pytestmark = pytest.mark.no_pg


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.add_url_rule("/login", endpoint="login", view_func=lambda: "")
    return app


def _seed_session():
    session.update(
        {
            "platform_user_id": 42,
            "platform_session_token": "platform-token",
            "user_id": 7,
            "session_token": "business-token",
        }
    )
    g.current_business = {"id": 1, "slug": "el-corte"}


def _assert_platform_session_preserved():
    assert session["platform_user_id"] == 42
    assert session["platform_session_token"] == "platform-token"
    assert "user_id" not in session
    assert "session_token" not in session


def test_admin_gate_preserves_platform_session_without_business_login(monkeypatch):
    app = _app()
    monkeypatch.setattr(admin_routes, "_is_authenticated", lambda: False)

    with app.test_request_context("/admin"):
        _seed_session()
        response = admin_routes._require_admin_membership()

        assert response.status_code == 302
        assert response.location == "/login"
        _assert_platform_session_preserved()


def test_admin_gate_preserves_platform_session_for_non_admin_membership(monkeypatch):
    app = _app()
    monkeypatch.setattr(admin_routes, "_is_authenticated", lambda: True)
    monkeypatch.setattr(admin_routes, "get_current_business_id", lambda: 1)
    monkeypatch.setattr(
        admin_routes, "get_membership_scoped", lambda user_id, business_id: {"role_name": "staff"}
    )

    with app.test_request_context("/admin"):
        _seed_session()
        response = admin_routes._require_admin_membership()

        assert response.status_code == 302
        assert response.location == "/login"
        _assert_platform_session_preserved()


def test_login_required_preserves_platform_session(monkeypatch):
    app = _app()
    monkeypatch.setattr(auth_routes, "_is_authenticated", lambda: False)
    protected = auth_routes.login_required(lambda: "protected")

    with app.test_request_context("/admin"):
        _seed_session()
        response = protected()

        assert response.status_code == 302
        assert response.location == "/login"
        _assert_platform_session_preserved()


def test_membership_required_preserves_platform_session_on_role_denial(monkeypatch):
    app = _app()
    monkeypatch.setattr(admin_routes, "_is_authenticated", lambda: True)
    monkeypatch.setattr(admin_routes, "get_current_business_id", lambda: 1)
    monkeypatch.setattr(
        admin_routes, "get_membership_scoped", lambda user_id, business_id: {"role_name": "staff"}
    )
    protected = admin_routes.membership_required("owner")(lambda: "protected")

    with app.test_request_context("/admin"):
        _seed_session()
        response = protected()

        assert response.status_code == 302
        assert response.location == "/login"
        _assert_platform_session_preserved()
