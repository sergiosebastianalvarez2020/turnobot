"""Pruebas mock de la configuración IA del Superadmin."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.no_pg


@pytest.fixture
def ia_env(monkeypatch):
    import app as application
    import routes.superadmin as superadmin_routes

    monkeypatch.setattr(superadmin_routes, "_superadmin_gate", lambda: None)
    monkeypatch.setattr(
        superadmin_routes,
        "_platform_current_user",
        lambda: ({"id": 1, "email": "root@example.com"}, None),
    )
    monkeypatch.setattr(superadmin_routes, "valid_csrf_token", lambda token: True)
    monkeypatch.setattr(superadmin_routes, "_check_ia_rate_limit", lambda client_ip, endpoint: True)
    monkeypatch.setattr(superadmin_routes, "get_client_ip", lambda: "127.0.0.1")
    return application.app, superadmin_routes


def test_ia_redirect_encodes_error(ia_env):
    _application, superadmin_routes = ia_env

    response = superadmin_routes._ia_redirect(error="falló & <detalle>")

    assert response.status_code == 302
    assert "%26" in response.headers["Location"]
    assert "%3C" in response.headers["Location"]


def test_guardar_uses_next_priority_without_duplicate_audit(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    create = MagicMock(return_value=7)
    audit = MagicMock()

    monkeypatch.setattr(
        superadmin_routes, "list_ai_providers", lambda: [{"priority": 1}, {"priority": 2}]
    )
    monkeypatch.setattr(superadmin_routes, "create_ai_provider", create)
    monkeypatch.setattr(superadmin_routes.platform_service, "log_platform_action", audit)

    data = {
        "csrf_token": "token",
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "active": "1",
        "api_key": "sk-test-key",
    }
    with application.test_request_context("/superadmin/ia/guardar", method="POST", data=data):
        response = superadmin_routes.superadmin_ia_guardar()

    assert response.status_code == 302
    assert "message=" in response.headers["Location"]
    assert create.call_args.kwargs["priority"] == 3
    audit.assert_not_called()


def test_probar_non_gemini_uses_decrypted_key_and_model(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    provider = {"id": 9, "provider": "groq", "model": "modelo-groq", "has_credentials": True}
    generate = MagicMock(return_value=(MagicMock(text="OK"), 1, None))
    audit = MagicMock()

    monkeypatch.setattr(superadmin_routes, "get_ai_provider", lambda pid: provider)
    monkeypatch.setattr(superadmin_routes, "get_decrypted_api_key", lambda pid: "clave-descifrada")
    monkeypatch.setattr(superadmin_routes.GroqProvider, "generate", generate)
    monkeypatch.setattr(superadmin_routes.platform_service, "log_platform_action", audit)

    with application.test_request_context(
        "/superadmin/ia/9/probar", method="POST", data={"csrf_token": "token"}
    ):
        response = superadmin_routes.superadmin_ia_probar(9)

    assert response.status_code == 302
    assert "message=" in response.headers["Location"]
    assert generate.call_args.kwargs["api_key"] == "clave-descifrada"
    assert generate.call_args.kwargs["model"] == "modelo-groq"
    audit.assert_called_once()


def test_ia_context_prefills_editing_provider(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    provider = {
        "id": 9,
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "priority": 2,
        "active": True,
    }

    monkeypatch.setattr(superadmin_routes, "list_ai_providers", lambda: [provider])
    monkeypatch.setattr(superadmin_routes, "get_ai_provider", lambda pid: provider)

    with application.test_request_context("/superadmin/ia?edit=9"):
        context = superadmin_routes._ia_context()

    assert context["editing_provider"] == provider
    assert context["section"] == "ia"


def test_guardar_crear_provider_activo(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    create = MagicMock(return_value=10)
    monkeypatch.setattr(superadmin_routes, "list_ai_providers", lambda: [])
    monkeypatch.setattr(superadmin_routes, "create_ai_provider", create)

    data = {
        "csrf_token": "token",
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "priority": "1",
        "active": "1",
        "api_key": "sk-test-key",
    }
    with application.test_request_context("/superadmin/ia/guardar", method="POST", data=data):
        response = superadmin_routes.superadmin_ia_guardar()

    assert response.status_code == 302
    assert create.call_args.kwargs["active"] is True
    assert create.call_args.kwargs["priority"] == 1


def test_guardar_crear_provider_desmarcado_inactivo(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    create = MagicMock(return_value=11)
    monkeypatch.setattr(superadmin_routes, "list_ai_providers", lambda: [])
    monkeypatch.setattr(superadmin_routes, "create_ai_provider", create)

    data = {
        "csrf_token": "token",
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "priority": "2",
        "active": "0",
        "api_key": "sk-test-key",
    }
    with application.test_request_context("/superadmin/ia/guardar", method="POST", data=data):
        response = superadmin_routes.superadmin_ia_guardar()

    assert response.status_code == 302
    assert create.call_args.kwargs["active"] is False


def test_guardar_editar_provider_activo_desmarcado(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    update = MagicMock(return_value=True)
    monkeypatch.setattr(superadmin_routes, "list_ai_providers", lambda: [{"priority": 1}])
    monkeypatch.setattr(superadmin_routes, "update_ai_provider", update)

    data = {
        "csrf_token": "token",
        "provider_id": "9",
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "priority": "1",
        "active": "0",
    }
    with application.test_request_context("/superadmin/ia/guardar", method="POST", data=data):
        response = superadmin_routes.superadmin_ia_guardar()

    assert response.status_code == 302
    assert update.call_args.kwargs["active"] is False


def test_guardar_editar_provider_inactivo_marcado(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    update = MagicMock(return_value=True)
    monkeypatch.setattr(superadmin_routes, "list_ai_providers", lambda: [{"priority": 1}])
    monkeypatch.setattr(superadmin_routes, "update_ai_provider", update)

    data = {
        "csrf_token": "token",
        "provider_id": "9",
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "priority": "1",
        "active": "1",
    }
    with application.test_request_context("/superadmin/ia/guardar", method="POST", data=data):
        response = superadmin_routes.superadmin_ia_guardar()

    assert response.status_code == 302
    assert update.call_args.kwargs["active"] is True


def test_guardar_editar_campo_active_ausente_preserva(ia_env, monkeypatch):
    application, superadmin_routes = ia_env
    update = MagicMock(return_value=True)
    monkeypatch.setattr(superadmin_routes, "list_ai_providers", lambda: [{"priority": 1}])
    monkeypatch.setattr(superadmin_routes, "update_ai_provider", update)

    data = {
        "csrf_token": "token",
        "provider_id": "9",
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "priority": "1",
    }
    with application.test_request_context("/superadmin/ia/guardar", method="POST", data=data):
        response = superadmin_routes.superadmin_ia_guardar()

    assert response.status_code == 302
    assert update.call_args.kwargs["active"] is None
