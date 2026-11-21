"""Pruebas del adaptador OpenAI-compatible y del router multi-proveedor."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.no_pg


class _Response:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def _client_sequence(responses):
    clients = []
    for response in responses:
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value = response
        clients.append(client)
    return clients


def _fake_client(captured, response_payload):
    class FakeResponse:
        status_code = 200

        def json(self):
            return response_payload

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    return FakeClient


def test_groq_generate_uses_model_system_and_tool_messages(monkeypatch):
    from services.ai_providers.groq import GroqProvider

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured: dict = {}
    monkeypatch.setattr(
        "services.ai_providers.openai_compatible.httpx.Client",
        _fake_client(
            captured,
            {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"total_tokens": 3},
            },
        ),
    )

    config = MagicMock()
    config.temperature = 0.2
    config.max_output_tokens = 50
    config.model = None
    declaration = MagicMock()
    declaration.name = "reservar"
    declaration.description = "Reserva un turno"
    declaration.parameters_json_schema = {"type": "object", "properties": {}}
    tool = MagicMock()
    tool.function_declarations = [declaration]
    config.tools = [tool]
    config.system_instruction = MagicMock()
    config.system_instruction.parts = [MagicMock(text="Sos útil")]

    contents = [
        {"role": "user", "parts": [{"text": "Hola"}]},
        {
            "role": "model",
            "parts": [{"function_call": {"name": "reservar", "args": {"dia": 1}, "id": "call_1"}}],
        },
        {
            "role": "user",
            "parts": [{"function_response": {"name": "reservar", "response": {"ok": True}}}],
        },
    ]

    response, attempts, error = GroqProvider().generate(
        contents, config, model="modelo-custom", timeout_s=3.0
    )

    assert error is None
    assert attempts == 1
    assert response.text == "OK"
    assert captured["json"]["model"] == "modelo-custom"
    assert captured["json"]["tool_choice"] == "auto"
    assert captured["timeout"] <= 3.0

    messages = captured["json"]["messages"]
    assert messages[0] == {"role": "system", "content": "Sos útil"}
    assert any(
        msg.get("role") == "tool" and msg.get("tool_call_id") == "call_1" for msg in messages
    )


def test_groq_expired_deadline_does_not_call_http(monkeypatch):
    from services.ai_providers.groq import GroqProvider

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    with patch("services.ai_providers.openai_compatible.httpx.Client") as client:
        response, _attempts, error = GroqProvider().generate(
            [], None, timeout_s=5.0, deadline_s=time.monotonic() - 1
        )

    assert response is None
    assert error == ("timeout", "GlobalTimeout")
    client.assert_not_called()


def test_openrouter_and_cerebras_presets_are_configurable(monkeypatch):
    from services.ai_providers.groq import CerebrasProvider, OpenRouterProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://or.example/v1/")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cb-key")
    monkeypatch.setenv("CEREBRAS_BASE_URL", "https://cb.example/v1/")

    openrouter = OpenRouterProvider()
    cerebras = CerebrasProvider()

    assert openrouter.is_configured() is True
    assert openrouter._chat_endpoint == "https://or.example/v1/chat/completions"
    assert cerebras.is_configured() is True
    assert cerebras._chat_endpoint == "https://cb.example/v1/chat/completions"


def test_router_registers_new_providers_and_skips_inactive_gemini(monkeypatch):
    from services.ai_router import AIProviderRouter

    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.setattr(
        "services.ai_router.list_ai_providers",
        lambda: [{"provider": "gemini", "priority": 1, "active": False}],
    )

    router = AIProviderRouter()
    router.clear_cache()

    assert {"openrouter", "cerebras"} <= set(router._PROVIDER_CLASSES)
    assert router._build_provider_list([], None, None) == []


def test_config_mutation_invalidates_router_cache(sqlite_temp_db, monkeypatch):
    from services.ai_config import create_ai_provider
    from services.ai_router import router

    monkeypatch.setenv("AI_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.setattr("services.ai_router.list_ai_providers", lambda: [])
    router.clear_cache()
    assert router._get_active_providers() == []

    create_ai_provider(
        provider="groq",
        api_key="sk-test-key",
        model="llama-3.1-8b-instant",
        priority=1,
        active=True,
    )

    assert router._providers_cache is None
    assert router._all_providers_cache is None


@pytest.mark.parametrize(
    "payload, expected_code",
    [
        ({}, "EmptyChoices"),
        ({"choices": []}, "EmptyChoices"),
        ({"choices": [None]}, "MalformedChoice"),
        ({"choices": [{}]}, "MissingMessage"),
        ({"choices": [{"message": {}}]}, "EmptyResponse"),
        ({"choices": [{"message": {"content": 123}}]}, "MalformedContent"),
    ],
)
def test_http_200_malformed_payloads_retry_and_classify(monkeypatch, payload, expected_code):
    from services.ai_providers.groq import GroqProvider

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    clients = _client_sequence([_Response(payload=payload), _Response(payload=payload)])
    monkeypatch.setattr(
        "services.ai_providers.openai_compatible.httpx.Client",
        lambda *args, **kwargs: clients.pop(0),
    )
    monkeypatch.setattr("services.ai_providers.openai_compatible.time.sleep", lambda _: None)

    response, attempts, error = GroqProvider().generate([], None, timeout_s=1.0)

    assert response is None
    assert attempts == 2
    assert error == ("malformed_response", expected_code)


def test_http_200_invalid_json_is_malformed_response(monkeypatch):
    from services.ai_providers.groq import GroqProvider

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    response = _Response(json_error=ValueError("invalid json"))
    clients = _client_sequence([response, response])
    monkeypatch.setattr(
        "services.ai_providers.openai_compatible.httpx.Client",
        lambda *args, **kwargs: clients.pop(0),
    )
    monkeypatch.setattr("services.ai_providers.openai_compatible.time.sleep", lambda _: None)

    result, attempts, error = GroqProvider().generate([], None, timeout_s=1.0)

    assert result is None
    assert attempts == 2
    assert error == ("malformed_response", "MalformedJSON")


def test_http_500_retries_then_success_counts_real_http_requests(monkeypatch):
    from services.ai_providers.groq import GroqProvider

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    payload = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
    clients = _client_sequence([_Response(status_code=500, payload={}), _Response(payload=payload)])
    monkeypatch.setattr(
        "services.ai_providers.openai_compatible.httpx.Client",
        lambda *args, **kwargs: clients.pop(0),
    )
    monkeypatch.setattr("services.ai_providers.openai_compatible.time.sleep", lambda _: None)

    response, attempts, error = GroqProvider().generate([], None, timeout_s=1.0)

    assert response is not None
    assert response.text == "OK"
    assert attempts == 2
    assert error is None


def test_provider_errors_never_log_api_key(monkeypatch, caplog):
    import logging

    from services.ai_providers.groq import GroqProvider

    caplog.set_level(logging.INFO, logger="turnobot.ai.groq")
    monkeypatch.setenv("GROQ_API_KEY", "super-secreto-local")
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = _Response(
        status_code=401, payload={"error": {"code": "invalid_api_key"}}
    )
    monkeypatch.setattr(
        "services.ai_providers.openai_compatible.httpx.Client", lambda *args, **kwargs: client
    )

    response, _attempts, error = GroqProvider().generate([], None, timeout_s=1.0)

    assert response is None
    assert error == ("auth", "invalid_api_key")
    assert "super-secreto-local" not in caplog.text
    assert "Authorization" not in caplog.text


def test_router_undecryptable_credentials_skip_provider_without_http(monkeypatch):
    from services.ai_router import AIProviderRouter

    provider_config = {
        "id": 1,
        "provider": "groq",
        "priority": 1,
        "active": True,
        "has_credentials": True,
        "api_key_encrypted": "enc",
        "model": "llama-3.1-8b-instant",
    }
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("services.ai_router.list_ai_providers", lambda: [provider_config])
    monkeypatch.setattr("services.ai_router.get_ai_provider_by_name", lambda name: provider_config)
    monkeypatch.setattr("services.ai_router.get_decrypted_api_key", lambda provider_id: None)

    with patch("services.ai_providers.groq.GroqProvider.generate") as generate:
        response, attempts, error = AIProviderRouter().generate([], None, business_id=1)

    assert response is None
    assert attempts == 0
    assert error is not None
    assert error.category == "unknown"
    generate.assert_not_called()


def test_router_passes_resolved_key_without_instantiation_key(monkeypatch):
    from services.ai_router import AIProviderRouter

    provider_config = {
        "id": 1,
        "provider": "groq",
        "priority": 1,
        "active": True,
        "has_credentials": True,
        "api_key_encrypted": "enc",
        "model": "llama-3.1-8b-instant",
    }
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("services.ai_router.list_ai_providers", lambda: [provider_config])
    monkeypatch.setattr("services.ai_router.get_ai_provider_by_name", lambda name: provider_config)
    monkeypatch.setattr(
        "services.ai_router.get_decrypted_api_key", lambda provider_id: "sk-decrypted"
    )

    router = AIProviderRouter()
    provider = MagicMock()
    provider.generate.return_value = (MagicMock(), 1, None)
    with patch.object(router, "_instantiate_provider", return_value=provider) as instantiate:
        response, attempts, error = router.generate([], None, business_id=1)

    instantiate.assert_called_once_with("groq")
    assert provider.generate.call_args.kwargs["api_key"] == "sk-decrypted"
    assert response is not None
    assert attempts == 1
    assert error is None


def test_router_shares_deadline_and_counts_http_across_fallback(monkeypatch):
    import time

    from services.ai_router import AIProviderRouter

    monkeypatch.setenv("AI_ENCRYPTION_KEY", "x" * 32)
    providers = [
        {
            "id": 1,
            "provider": "gemini",
            "priority": 1,
            "active": True,
            "has_credentials": True,
            "api_key_encrypted": "enc",
            "model": "gemini-2.5-flash",
        },
        {
            "id": 2,
            "provider": "groq",
            "priority": 2,
            "active": True,
            "has_credentials": True,
            "api_key_encrypted": "enc",
            "model": "llama-3.1-8b-instant",
        },
    ]
    monkeypatch.setattr("services.ai_router.list_ai_providers", lambda: providers)
    monkeypatch.setattr(
        "services.ai_router.get_ai_provider_by_name",
        lambda name: next(p for p in providers if p["provider"] == name),
    )
    monkeypatch.setattr("services.ai_router.get_decrypted_api_key", lambda provider_id: "key")

    seen = {}

    def gemini_generate(contents, config, **kwargs):
        seen["gemini_deadline"] = kwargs["deadline_s"]
        return None, 2, ("server", "UNAVAILABLE")

    def groq_generate(contents, config, **kwargs):
        seen["groq_deadline"] = kwargs["deadline_s"]
        response = MagicMock()
        response.text = "OK"
        return response, 1, None

    with (
        patch("services.ai_providers.gemini.GeminiProvider.generate", side_effect=gemini_generate),
        patch("services.ai_providers.groq.GroqProvider.generate", side_effect=groq_generate),
    ):
        response, attempts, error = AIProviderRouter().generate([], None, business_id=1)

    before = time.monotonic()
    assert response is not None
    assert response.text == "OK"
    assert attempts == 3
    assert error is None
    assert seen["gemini_deadline"] == seen["groq_deadline"]
    assert seen["gemini_deadline"] > before
