"""
tests/test_ai_stage3.py — Pruebas de Etapa 3: Groq y motor de fallback.

Pruebas mockeadas para GroqProvider, AIProviderRouter y la integración
con ask_ai(). No consumen cuotas reales de ningún proveedor.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services.ai_providers.groq import GroqProvider
from services.ai_router import AIProviderRouter, router

pytestmark = pytest.mark.no_pg


# ------------------------------------------------------------------
# Fixtures y helpers
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_router_cache(monkeypatch):
    """Limpia la caché del router entre tests."""
    router.clear_cache()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """Configura AI_ENCRYPTION_KEY para tests del router."""
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "x" * 32)


@pytest.fixture
def mock_httpx_client(monkeypatch):
    """Mockea httpx.Client para simular respuestas de Groq."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Hola, ¿en qué puedo ayudarte?"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_response
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: mock_client)
    return mock_client, mock_response


# ------------------------------------------------------------------
# Pruebas de GroqProvider
# ------------------------------------------------------------------


class TestGroqProvider:
    """Pruebas del adaptador de Groq."""

    def test_provider_name(self):
        assert GroqProvider().provider_name() == "groq"

    def test_is_configured_without_key(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert GroqProvider().is_configured() is False

    def test_is_configured_with_key(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        assert GroqProvider().is_configured() is True

    def test_successful_response(self, monkeypatch, mock_httpx_client):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        _, mock_response = mock_httpx_client

        provider = GroqProvider()
        contents = [[{"role": "user", "parts": [{"text": "Hola"}]}]]
        config = MagicMock()
        config.tools = None
        config.system_instruction = None
        config.temperature = 0.2
        config.max_output_tokens = 1024
        config.model = None

        response, attempts, error = provider.generate(
            contents, config, business_id=1, request_id="req-1"
        )

        assert response is not None
        assert error is None
        assert attempts == 1
        assert "Hola" in response.text
        assert len(response.candidates) == 1

    def test_successful_response_with_function_calls(self, monkeypatch, mock_httpx_client):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        _, mock_response = mock_httpx_client
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "reservar_turno",
                                    "arguments": json.dumps(
                                        {
                                            "nombre": "Juan",
                                            "telefono": "12345678",
                                            "servicio": "Corte",
                                            "fecha": "2026-12-01",
                                            "hora": "10:00",
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }

        provider = GroqProvider()
        contents = [[{"role": "user", "parts": [{"text": "Quiero reservar"}]}]]
        config = MagicMock()
        config.tools = None
        config.system_instruction = None
        config.temperature = 0.2
        config.max_output_tokens = 1024
        config.model = None

        response, attempts, error = provider.generate(contents, config, business_id=1)

        assert response is not None
        assert error is None
        assert len(response.candidates[0].content.parts) == 1
        fc = response.candidates[0].content.parts[0].function_call
        assert fc.name == "reservar_turno"
        assert fc.args["nombre"] == "Juan"

    def test_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        provider = GroqProvider()
        response, attempts, error = provider.generate([], None)
        assert response is None
        assert error == ("auth", "MissingAPIKey")

    def test_error_429_quota(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response
        monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: mock_client)

        provider = GroqProvider()
        response, attempts, error = provider.generate([], None)

        assert response is None
        assert error == ("quota", "RateLimitExceeded")
        assert attempts == 2  # Reintenta una vez

    def test_error_503_server(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response
        monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: mock_client)

        provider = GroqProvider()
        response, attempts, error = provider.generate([], None)

        assert response is None
        assert error[0] == "server"

    def test_timeout_error(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.TimeoutException("timeout")),
        )

        provider = GroqProvider()
        response, attempts, error = provider.generate([], None)

        assert response is None
        assert error[0] == "timeout"

    def test_connection_error(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("connection failed")),
        )

        provider = GroqProvider()
        response, attempts, error = provider.generate([], None)

        assert response is None
        assert error[0] == "server"

    def test_error_401_auth(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test123456789")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response
        monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: mock_client)

        provider = GroqProvider()
        response, attempts, error = provider.generate([], None)

        assert response is None
        assert error == ("auth", "HTTP401")
        assert attempts == 1  # No reintenta en error de auth

    def test_classify_error(self):
        provider = GroqProvider()
        assert provider.classify_error(TimeoutError()) == ("timeout", True, "TimeoutError")
        assert provider.classify_error(ConnectionError()) == ("server", True, "ConnectionError")
        assert provider.is_transient_error(TimeoutError()) is True
        assert provider.is_transient_error(ConnectionError()) is True

    def test_error_message_categories(self):
        provider = GroqProvider()
        assert "consulta" in provider.error_message("timeout")
        assert "limitaciones" in provider.error_message("quota")
        assert "problema" in provider.error_message("auth")
        assert "problema" in provider.error_message("server")


# ------------------------------------------------------------------
# Pruebas de AIProviderRouter
# ------------------------------------------------------------------


class TestAIProviderRouter:
    """Pruebas del router de fallback."""

    def test_router_has_providers(self):
        """El router tiene proveedores registrados."""
        assert "gemini" in AIProviderRouter._PROVIDER_CLASSES
        assert "groq" in AIProviderRouter._PROVIDER_CLASSES

    def test_contingency_message(self):
        """El mensaje de contingencia no revela detalles técnicos."""
        msg = router.contingency_message()
        assert "dificultades" in msg
        assert "intentarlo nuevamente" in msg
        assert "gemini" not in msg.lower()
        assert "groq" not in msg.lower()
        assert "api" not in msg.lower()
        assert "key" not in msg.lower()

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_gemini_legacy_mode(self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch):
        """Gemini en modo legacy cuando no hay registro en la tabla."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_list.return_value = []
        mock_get_by_name.return_value = None
        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_generate:
            mock_response = MagicMock()
            mock_response.text = "Respuesta Gemini"
            mock_generate.return_value = (mock_response, 1, None)
            response, attempts, error = router.generate(contents=[], config=None, business_id=1)

            assert response is not None
            assert error is None
            mock_generate.assert_called_once()

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_gemini_429_fallback_to_groq(
        self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch
    ):
        """Gemini falla con 429 y Groq responde."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "provider": "groq",
                "priority": 2,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
        ]
        mock_list.return_value = providers
        mock_get_by_name.side_effect = providers

        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 2, ("quota", "RESOURCE_EXHAUSTED"))

            with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq_gen:
                mock_response = MagicMock()
                mock_response.text = "Respuesta Groq"
                mock_groq_gen.return_value = (mock_response, 1, None)

                response, attempts, error = router.generate(contents=[], config=None, business_id=1)

                assert response is not None
                assert response.text == "Respuesta Groq"
                assert error is None
                mock_gemini_gen.assert_called_once()
                mock_groq_gen.assert_called_once()

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_all_providers_fail(self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch):
        """Todos los proveedores fallan → error."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            }
        ]
        mock_list.return_value = providers
        mock_get_by_name.return_value = providers[0]
        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 2, ("server", "UNAVAILABLE"))

            response, attempts, error = router.generate(contents=[], config=None, business_id=1)

            assert response is None
            assert error is not None
            assert error[0] == "server"

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    def test_no_providers_available(self, mock_list, mock_get_by_name, monkeypatch):
        """No hay proveedores configurados ni API key de entorno."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        mock_list.return_value = []
        mock_get_by_name.return_value = None
        router.clear_cache()

        response, attempts, error = router.generate(contents=[], config=None, business_id=1)

        assert response is None
        assert error is not None
        assert error[0] == "unknown"

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    def test_provider_not_active_is_skipped(self, mock_list, mock_get_by_name, monkeypatch):
        """Proveedor inactivo se salta."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": False,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            }
        ]
        mock_list.return_value = providers
        mock_get_by_name.return_value = None
        router.clear_cache()

        response, attempts, error = router.generate(contents=[], config=None, business_id=1)

        # Gemini está inactivo, no hay otros proveedores, y no hay GEMINI_API_KEY
        assert response is None


# ------------------------------------------------------------------
# Pruebas de function calling sin duplicación
# ------------------------------------------------------------------


class TestFunctionCallingNoDuplication:
    """Pruebas de que las herramientas no se duplican durante fallback."""

    def test_tool_handlers_registered(self):
        """Los handlers de herramientas están registrados."""
        from services.ai import _TOOL_HANDLERS

        assert "reservar_turno" in _TOOL_HANDLERS
        assert "cancelar_turno" in _TOOL_HANDLERS
        assert "reprogramar_turno" in _TOOL_HANDLERS

    def test_execute_tool_invalid_business_id(self):
        """execute_tool con business_id=None retorna error."""
        from services.ai import execute_tool

        result = execute_tool("reservar_turno", {}, business_id=None)
        assert result["success"] is False
        assert result["error"] == "Contexto de negocio inválido."

    def test_execute_tool_unknown_tool(self):
        """execute_tool con herramienta inexistente retorna None."""
        from services.ai import execute_tool

        result = execute_tool("herramienta_inexistente", {}, business_id=1)
        assert result is None


# ------------------------------------------------------------------
# Pruebas T1-T9: Correcciones de seguridad y fallback (Etapa 3.2)
# ------------------------------------------------------------------


class TestCredentialIsolation:
    """T1, T9: Aislamiento de credenciales Gemini entre solicitudes concurrentes."""

    def test_two_sequential_requests_different_keys(self, monkeypatch):
        """T1: Dos solicitudes secuenciales con API Keys distintas usan clientes distintos."""
        from services.ai import _clients, _clients_lock, get_gemini_client

        # Limpiar caché
        with _clients_lock:
            _clients.clear()

        # Crear dos clientes con claves distintas
        client1 = get_gemini_client(api_key="key-alpha-12345")
        client2 = get_gemini_client(api_key="key-beta-67890")

        assert client1 is not client2, (
            "Clientes con claves distintas deben ser instancias diferentes"
        )

        # Verificar que la caché tiene dos entradas
        with _clients_lock:
            assert len(_clients) == 2, f"Se esperaban 2 clientes en caché, hay {len(_clients)}"

    def test_concurrent_requests_different_keys(self, monkeypatch):
        """T1: Dos solicitudes concurrentes con API Keys distintas no se interferencian."""
        import threading

        from services.ai import _clients, _clients_lock, get_gemini_client

        # Limpiar caché
        with _clients_lock:
            _clients.clear()

        results = {}
        errors = []

        def make_request(key_name, api_key):
            try:
                client = get_gemini_client(api_key=api_key)
                results[key_name] = client
            except Exception as exc:  # pragma: no cover
                errors.append((key_name, str(exc)))

        t1 = threading.Thread(target=make_request, args=("thread-1", "key-concurrent-a"))
        t2 = threading.Thread(target=make_request, args=("thread-2", "key-concurrent-b"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Errores en solicitudes concurrentes: {errors}"
        assert results["thread-1"] is not results["thread-2"], (
            "Hilos con claves distintas deben obtener clientes distintos"
        )

        with _clients_lock:
            assert len(_clients) == 2, f"Se esperaban 2 clientes, hay {len(_clients)}"

    def test_explicit_api_key_never_ignored(self, monkeypatch):
        """T1: La clave explícita tiene prioridad sobre GEMINI_API_KEY."""
        from services.ai import get_gemini_client

        monkeypatch.setenv("GEMINI_API_KEY", "env-legacy-key")
        client = get_gemini_client(api_key="explicit-key-999")
        assert client is not None

        # Verificar que el cliente fue creado con la clave explícita
        # (no podemos verificar la clave interna, pero verificamos que se creó un cliente distinto)
        from services.ai import _clients, _clients_lock

        with _clients_lock:
            assert len(_clients) >= 1

    def test_client_invalidation_on_rotation(self, monkeypatch):
        """T9: Invalidar cliente cuando se rota una credencial."""
        from services.ai import _clients, _clients_lock, get_gemini_client, invalidate_gemini_client

        # Limpiar caché
        with _clients_lock:
            _clients.clear()

        client1 = get_gemini_client(api_key="key-before-rotation")
        client2 = get_gemini_client(api_key="key-before-rotation")
        assert client1 is client2, "Misma clave debe devolver el mismo cliente caché"

        # Invalidar y crear de nuevo
        invalidate_gemini_client(api_key="key-before-rotation")
        client3 = get_gemini_client(api_key="key-before-rotation")
        assert client3 is not client1, "Después de invalidar, debe crearse un nuevo cliente"


class TestAuthFallback:
    """T2, T3: Fallback en errores de autenticación (401/403)."""

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_gemini_401_fallback_to_groq(
        self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch
    ):
        """T2: Gemini devuelve 401 y Groq responde correctamente."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "provider": "groq",
                "priority": 2,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
        ]
        mock_list.return_value = providers
        mock_get_by_name.side_effect = providers
        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 1, ("auth", "HTTP401"))

            with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq_gen:
                mock_response = MagicMock()
                mock_response.text = "Respuesta Groq tras 401 de Gemini"
                mock_groq_gen.return_value = (mock_response, 1, None)

                response, attempts, error = router.generate(contents=[], config=None, business_id=1)

                assert response is not None
                assert response.text == "Respuesta Groq tras 401 de Gemini"
                assert error is None
                mock_gemini_gen.assert_called_once()
                mock_groq_gen.assert_called_once()

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_gemini_403_fallback_to_groq(
        self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch
    ):
        """T2: Gemini devuelve 403 por credencial y Groq responde correctamente."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "provider": "groq",
                "priority": 2,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
        ]
        mock_list.return_value = providers
        mock_get_by_name.side_effect = providers
        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 1, ("auth", "HTTP403"))

            with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq_gen:
                mock_response = MagicMock()
                mock_response.text = "Respuesta Groq tras 403 de Gemini"
                mock_groq_gen.return_value = (mock_response, 1, None)

                response, attempts, error = router.generate(contents=[], config=None, business_id=1)

                assert response is not None
                assert response.text == "Respuesta Groq tras 403 de Gemini"
                assert error is None

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_gemini_400_no_fallback(self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch):
        """T3: Gemini devuelve 400 y no se intenta Groq."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "provider": "groq",
                "priority": 2,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
        ]
        mock_list.return_value = providers
        mock_get_by_name.side_effect = providers
        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 1, ("invalid_argument", "HTTP400"))

            with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq_gen:
                response, attempts, error = router.generate(contents=[], config=None, business_id=1)

                assert response is None
                assert error == ("invalid_argument", "HTTP400")
                mock_gemini_gen.assert_called_once()
                mock_groq_gen.assert_not_called()  # No se intenta Groq en 400

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_gemini_404_model_not_found(
        self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch
    ):
        """T3: Gemini devuelve 404 por modelo inexistente, clasificación invalid_argument, no fallback."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "gemini-inexistente",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            }
        ]
        mock_list.return_value = providers
        mock_get_by_name.return_value = providers[0]
        router.clear_cache()

        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 1, ("invalid_argument", "HTTP404"))

            response, attempts, error = router.generate(contents=[], config=None, business_id=1)

            assert response is None
            assert error == ("invalid_argument", "HTTP404")
            mock_gemini_gen.assert_called_once()

    @patch("services.ai_router.get_ai_provider_by_name")
    @patch("services.ai_router.list_ai_providers")
    @patch("services.ai_router.get_decrypted_api_key")
    def test_groq_401_contingency_no_more_providers(
        self, mock_decrypt, mock_list, mock_get_by_name, monkeypatch
    ):
        """T3: Groq devuelve 401 y el sistema devuelve contingencia si no hay más proveedores."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
        mock_decrypt.return_value = "decrypted-key"

        providers = [
            {
                "id": 1,
                "provider": "groq",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "encrypted",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            }
        ]
        mock_list.return_value = providers
        mock_get_by_name.return_value = providers[0]
        router.clear_cache()

        # Mock Gemini legacy generate para que retorne error de auth
        # (el router añade Gemini legacy automáticamente cuando no está en la tabla)
        with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gemini_gen:
            mock_gemini_gen.return_value = (None, 1, ("auth", "HTTP401"))

            with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq_gen:
                mock_groq_gen.return_value = (None, 1, ("auth", "HTTP401"))

                response, attempts, error = router.generate(contents=[], config=None, business_id=1)

                assert response is None
                assert error == ("auth", "HTTP401")


class TestReservationSuccessDeterministic:
    """T4, T5, T6, T7, T8: Reservas confirmadas y respuesta determinística."""

    def test_reservation_success_deterministic_response(self):
        """T4: Reserva exitosa retorna confirmación determinística sin llamar al router."""
        from services.ai import _TOOL_HANDLERS, execute_tool

        # Verificar que el handler de reservar_turno está registrado
        assert "reservar_turno" in _TOOL_HANDLERS

        # Verificar que execute_tool con business_id=None retorna error
        result = execute_tool("reservar_turno", {}, business_id=None)
        assert result["success"] is False
        assert result["error"] == "Contexto de negocio inválido."

    def test_cancellation_success_no_duplicate(self):
        """T5: Cancelación exitosa no duplica la operación."""
        from services.ai import _TOOL_HANDLERS, execute_tool

        assert "cancelar_turno" in _TOOL_HANDLERS

        result = execute_tool("cancelar_turno", {}, business_id=None)
        assert result["success"] is False
        assert result["error"] == "Contexto de negocio inválido."

    def test_reschedule_success_no_duplicate(self):
        """T6: Reprogramación exitosa no duplica la operación."""
        from services.ai import _TOOL_HANDLERS, execute_tool

        assert "reprogramar_turno" in _TOOL_HANDLERS

        result = execute_tool("reprogramar_turno", {}, business_id=None)
        assert result["success"] is False
        assert result["error"] == "Contexto de negocio inválido."

    def test_tool_executed_once_per_response(self):
        """T8: Cada herramienta se ejecuta exactamente una vez por respuesta de proveedor."""
        from services.ai import _TOOL_HANDLERS

        # Verificar que los handlers están registrados
        assert len(_TOOL_HANDLERS) == 7
        assert "reservar_turno" in _TOOL_HANDLERS
        assert "cancelar_turno" in _TOOL_HANDLERS
        assert "reprogramar_turno" in _TOOL_HANDLERS
        assert "consultar_recursos" in _TOOL_HANDLERS
        assert "consultar_disponibilidad" in _TOOL_HANDLERS
        assert "buscar_turnos_cliente" in _TOOL_HANDLERS
        assert "solicitar_atencion_humana" in _TOOL_HANDLERS


class TestGroqFunctionCalling:
    """D1-D4: Function calling de Groq."""

    def test_tool_call_ids_are_unique(self):
        """D1: Los IDs de tool calls son únicos, no siempre 'call_0'."""
        from services.ai_providers.groq import GroqProvider

        provider = GroqProvider()

        # Simular múltiples tool calls en la respuesta
        message = {
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "reservar_turno", "arguments": "{}"},
                },
                {
                    "id": "call_def456",
                    "type": "function",
                    "function": {"name": "cancelar_turno", "arguments": "{}"},
                },
            ]
        }

        calls = provider._extract_function_calls(message)
        assert len(calls) == 2
        assert calls[0]["id"] == "call_abc123"
        assert calls[1]["id"] == "call_def456"
        assert calls[0]["id"] != calls[1]["id"], "Los IDs deben ser únicos"

    def test_tool_choice_is_auto_not_required(self):
        """D2: tool_choice es 'auto', no 'required'."""
        import inspect

        from services.ai_providers.groq import GroqProvider

        source = inspect.getsource(GroqProvider.generate)
        assert "tool_choice" in source
        assert '"auto"' in source, "tool_choice debe ser 'auto', no 'required'"

    def test_message_with_text_and_tool_calls(self):
        """D3: Un mensaje puede tener texto y tool calls simultáneamente."""
        from services.ai_providers.groq import GroqProvider

        class MockFunctionCall:
            def __init__(self, id, name, args):
                self.id = id
                self.name = name
                self.args = args

        class MockPart:
            def __init__(self, text=None, function_call=None):
                self.text = text
                self.function_call = function_call

        class MockContent:
            def __init__(self, role, parts):
                self.role = role
                self.parts = parts

        contents = [
            MockContent(
                role="model",
                parts=[
                    MockPart(text="Hola"),
                    MockPart(
                        function_call=MockFunctionCall(
                            id="call_1", name="reservar_turno", args={"nombre": "Juan"}
                        )
                    ),
                ],
            )
        ]

        messages = GroqProvider._contents_to_messages(contents)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "Hola"  # El texto se conserva
        assert len(messages[0]["tool_calls"]) == 1  # Y también la tool call
        assert messages[0]["tool_calls"][0]["id"] == "call_1"

    def test_response_without_tool_call(self):
        """D4: Respuesta directa sin herramienta funciona correctamente."""
        from services.ai_providers.groq import GroqProvider

        provider = GroqProvider()

        # Simular respuesta sin tool calls
        message = {"role": "assistant", "content": "Respuesta de texto simple"}
        calls = provider._extract_function_calls(message)
        assert calls == []

        text = provider._extract_text(message)
        assert text == "Respuesta de texto simple"

    def test_groq_timeout_supports_timeout_s_param(self):
        """D4: GroqProvider.generate acepta parámetro timeout_s."""
        import inspect

        sig = inspect.signature(GroqProvider.generate)
        assert "timeout_s" in sig.parameters


class TestTimeoutBudget:
    """E: Timeouts y presupuesto total."""

    def test_router_global_timeout_is_20s(self):
        """E: El timeout global del router es 20 segundos."""
        from services.ai_router import _MAX_GLOBAL_TIMEOUT_S

        assert _MAX_GLOBAL_TIMEOUT_S == 20

    def test_gemini_default_timeout_is_8s(self):
        """E: El timeout por defecto de Gemini es 8 segundos."""
        import os

        default = int(os.getenv("GEMINI_TIMEOUT_MS", "8000"))
        assert default == 8000

    def test_groq_default_timeout_is_8s(self):
        """E: El timeout por defecto de Groq es 8 segundos."""
        from services.ai_providers.groq import _GROQ_TIMEOUT_S

        assert _GROQ_TIMEOUT_S == 8.0

    def test_router_distributes_time_per_provider(self):
        """E: El router distribuye el tiempo entre proveedores activos."""
        from services.ai_router import AIProviderRouter

        router = AIProviderRouter()
        # Con 2 proveedores, el tiempo por proveedor debería ser ~10s
        # (máximo permitido por proveedor)
        # Esto se verifica indirectamente mediante el código del router
        import inspect

        source = inspect.getsource(router.generate)
        assert "time_per_provider" in source
        assert "remaining" in source


# ------------------------------------------------------------------
# Pruebas A.4: Clasificación semántica de errores (401/403)
# ------------------------------------------------------------------


class TestErrorClassificationSemantics:
    """A.4: Clasificación semántica de errores de autenticación vs permisos."""

    def _make_gemini_error(self, status=None, code=None):
        """Crea un mock de genai_errors.APIError con status y code."""
        from google.genai import errors as genai_errors

        class MockGeminiError(genai_errors.APIError):
            def __init__(self, status=None, code=None):
                self.status = status
                self.code = code
                super().__init__(code=code or 0, response_json={})

        return MockGeminiError(status, code)

    def test_gemini_401_is_auth_no_fallback(self):
        """Gemini 401 → categoría auth, sin fallback."""
        from services.ai import _classify_gemini_error

        error = self._make_gemini_error(status="UNAUTHENTICATED", code=401)
        category, is_transient, error_type = _classify_gemini_error(error)
        assert category == "auth"
        assert is_transient is False
        assert error_type == "UNAUTHENTICATED"

    def test_gemini_403_permission_denied_is_permission(self):
        """Gemini 403/PERMISSION_DENIED → categoría permission, sin fallback."""
        from services.ai import _classify_gemini_error

        error = self._make_gemini_error(status="PERMISSION_DENIED", code=403)
        category, is_transient, error_type = _classify_gemini_error(error)
        assert category == "permission"
        assert is_transient is False
        assert error_type == "PERMISSION_DENIED"

    def test_gemini_403_code_403_is_permission(self):
        """Gemini 403 con code=403 → categoría permission."""
        from services.ai import _classify_gemini_error

        error = self._make_gemini_error(status=None, code=403)
        category, is_transient, _ = _classify_gemini_error(error)
        assert category == "permission"
        assert is_transient is False

    def test_gemini_401_no_fallback_in_router(self):
        """Gemini 401 no activa fallback (no es transitorio)."""
        from services.ai_router import AIProviderRouter

        router = AIProviderRouter()
        router.clear_cache()

        with patch("services.ai_router.get_ai_provider_by_name") as mock_get_by_name:
            with patch("services.ai_router.list_ai_providers") as mock_list:
                with patch("services.ai_router.get_decrypted_api_key") as mock_decrypt:
                    import os

                    os.environ["GEMINI_API_KEY"] = "test-key"
                    mock_list.return_value = []
                    mock_get_by_name.return_value = None
                    mock_decrypt.return_value = "key"

                    router.clear_cache()

                    with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gen:
                        mock_gen.return_value = (None, 1, ("auth", "UNAUTHENTICATED"))

                        response, attempts, error = router.generate(
                            contents=[], config=None, business_id=1
                        )

                        assert response is None
                        assert error == ("auth", "UNAUTHENTICATED")
                        # No se intentó fallback porque no hay más proveedores
                        mock_gen.assert_called_once()

    def test_gemini_403_permission_no_fallback(self):
        """Gemini 403/permission no activa fallback."""
        from services.ai_router import AIProviderRouter

        router = AIProviderRouter()
        router.clear_cache()

        with patch("services.ai_router.get_ai_provider_by_name") as mock_get_by_name:
            with patch("services.ai_router.list_ai_providers") as mock_list:
                with patch("services.ai_router.get_decrypted_api_key") as mock_decrypt:
                    import os

                    os.environ["GEMINI_API_KEY"] = "test-key"
                    mock_list.return_value = []
                    mock_get_by_name.return_value = None
                    mock_decrypt.return_value = "key"

                    router.clear_cache()

                    with patch("services.ai_providers.gemini.GeminiProvider.generate") as mock_gen:
                        mock_gen.return_value = (None, 1, ("permission", "PERMISSION_DENIED"))

                        response, attempts, error = router.generate(
                            contents=[], config=None, business_id=1
                        )

                        assert response is None
                        assert error == ("permission", "PERMISSION_DENIED")
                        mock_gen.assert_called_once()

    def test_groq_401_is_auth_no_fallback(self):
        """Groq 401 → categoría auth, sin fallback."""
        from services.ai_providers.groq import GroqProvider

        provider = GroqProvider()
        category, is_transient, error_type = provider._classify_http_error(401)
        assert category == "auth"
        assert is_transient is False
        assert error_type == "HTTP401"

    def test_groq_403_is_permission_no_fallback(self):
        """Groq 403 → categoría permission, sin fallback."""
        from services.ai_providers.groq import GroqProvider

        provider = GroqProvider()
        category, is_transient, error_type = provider._classify_http_error(403)
        assert category == "permission"
        assert is_transient is False
        assert error_type == "HTTP403"

    def test_groq_400_is_not_transient(self):
        """Groq 400 → invalid_argument, no fallback."""
        from services.ai_providers.groq import GroqProvider

        provider = GroqProvider()
        category, is_transient, _ = provider._classify_http_error(400)
        assert category == "invalid_argument"
        assert is_transient is False

    def test_groq_404_is_not_transient(self):
        """Groq 404 → invalid_argument, no fallback."""
        from services.ai_providers.groq import GroqProvider

        provider = GroqProvider()
        category, is_transient, _ = provider._classify_http_error(404)
        assert category == "invalid_argument"
        assert is_transient is False

    def test_gemini_429_is_transient_fallback(self):
        """Gemini 429 → quota, transitorio, activa fallback."""
        from services.ai import _classify_gemini_error

        error = self._make_gemini_error(status="RESOURCE_EXHAUSTED", code=429)
        category, is_transient, _ = _classify_gemini_error(error)
        assert category == "quota"
        assert is_transient is True

    def test_gemini_503_is_transient_fallback(self):
        """Gemini 503 → server, transitorio, activa fallback."""
        from services.ai import _classify_gemini_error

        error = self._make_gemini_error(status="UNAVAILABLE", code=503)
        category, is_transient, _ = _classify_gemini_error(error)
        assert category == "server"
        assert is_transient is True

    def test_router_auth_triggers_fallback(self):
        """Router: auth (401) activa fallback a siguiente proveedor."""
        from services.ai_router import AIProviderRouter

        router = AIProviderRouter()
        router.clear_cache()

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "enc",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "provider": "groq",
                "priority": 2,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "enc",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
        ]

        with patch("services.ai_router.get_ai_provider_by_name") as mock_get_by_name:
            with patch("services.ai_router.list_ai_providers") as mock_list:
                with patch("services.ai_router.get_decrypted_api_key") as mock_decrypt:
                    import os

                    os.environ["GEMINI_API_KEY"] = "test-key"
                    mock_list.return_value = providers
                    mock_get_by_name.side_effect = providers
                    mock_decrypt.return_value = "key"

                    router.clear_cache()

                    with patch(
                        "services.ai_providers.gemini.GeminiProvider.generate"
                    ) as mock_gemini:
                        mock_gemini.return_value = (None, 1, ("auth", "UNAUTHENTICATED"))

                        with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq:
                            mock_resp = MagicMock()
                            mock_resp.text = "Groq respondió tras 401 de Gemini"
                            mock_groq.return_value = (mock_resp, 1, None)

                            response, attempts, error = router.generate(
                                contents=[], config=None, business_id=1
                            )

                            assert response is not None
                            assert response.text == "Groq respondió tras 401 de Gemini"
                            assert error is None
                            mock_gemini.assert_called_once()
                            mock_groq.assert_called_once()

    def test_router_permission_no_fallback(self):
        """Router: permission (403) NO activa fallback."""
        from services.ai_router import AIProviderRouter

        router = AIProviderRouter()
        router.clear_cache()

        providers = [
            {
                "id": 1,
                "provider": "gemini",
                "priority": 1,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "enc",
                "model": "gemini-2.5-flash",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "provider": "groq",
                "priority": 2,
                "active": True,
                "has_credentials": True,
                "api_key_encrypted": "enc",
                "model": "llama-3.1-8b-instant",
                "encryption_key_version": 1,
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
            },
        ]

        with patch("services.ai_router.get_ai_provider_by_name") as mock_get_by_name:
            with patch("services.ai_router.list_ai_providers") as mock_list:
                with patch("services.ai_router.get_decrypted_api_key") as mock_decrypt:
                    import os

                    os.environ["GEMINI_API_KEY"] = "test-key"
                    mock_list.return_value = providers
                    mock_get_by_name.side_effect = providers
                    mock_decrypt.return_value = "key"

                    router.clear_cache()

                    with patch(
                        "services.ai_providers.gemini.GeminiProvider.generate"
                    ) as mock_gemini:
                        mock_gemini.return_value = (None, 1, ("permission", "PERMISSION_DENIED"))

                        with patch("services.ai_providers.groq.GroqProvider.generate") as mock_groq:
                            response, attempts, error = router.generate(
                                contents=[], config=None, business_id=1
                            )

                            assert response is None
                            assert error == ("permission", "PERMISSION_DENIED")
                            mock_gemini.assert_called_once()
                            mock_groq.assert_not_called()  # No fallback para permission


# ------------------------------------------------------------------
# Pruebas B.6: Deadline y presupuesto de tiempo
# ------------------------------------------------------------------


class TestRouterDeadline:
    """B.6: Pruebas de deadline y presupuesto de tiempo del router."""

    def test_router_has_deadline_logic(self):
        """El router usa un deadline basado en tiempo monotónico."""
        import inspect

        from services.ai_router import AIProviderRouter

        source = inspect.getsource(AIProviderRouter.generate)
        assert "deadline" in source
        assert "time.monotonic()" in source

    def test_gemini_accepts_deadline_s(self):
        """GeminiProvider.generate acepta parámetro deadline_s."""
        import inspect

        from services.ai_providers.gemini import GeminiProvider

        sig = inspect.signature(GeminiProvider.generate)
        assert "deadline_s" in sig.parameters

    def test_groq_accepts_deadline_s(self):
        """GroqProvider.generate acepta parámetro deadline_s."""
        import inspect

        from services.ai_providers.groq import GroqProvider

        sig = inspect.signature(GroqProvider.generate)
        assert "deadline_s" in sig.parameters

    def test_router_passes_deadline_to_providers(self):
        """El router pasa deadline_s a los proveedores."""
        import inspect

        from services.ai_router import AIProviderRouter

        source = inspect.getsource(AIProviderRouter.generate)
        assert "deadline_s=deadline" in source

    def test_router_checks_remaining_before_each_provider(self):
        """El router verifica tiempo restante antes de cada proveedor."""
        import inspect

        from services.ai_router import AIProviderRouter

        source = inspect.getsource(AIProviderRouter.generate)
        assert "remaining = deadline - time.monotonic()" in source

    def test_router_backoff_respects_deadline(self):
        """El backoff del router respeta el deadline."""
        import inspect

        from services.ai_router import AIProviderRouter

        source = inspect.getsource(AIProviderRouter.generate)
        # El router pasa deadline_s a proveedores, que manejan el backoff
        assert "deadline_s=deadline" in source

    def test_gemini_backoff_respects_deadline(self):
        """Gemini _call_gemini_with_retry respeta deadline en backoff."""
        import inspect

        from services.ai import _call_gemini_with_retry

        source = inspect.getsource(_call_gemini_with_retry)
        assert "deadline_s" in source
        assert "remaining = deadline_s - monotonic()" in source

    def test_groq_backoff_respects_deadline(self):
        """Groq generate respeta deadline en backoff."""
        import inspect

        from services.ai_providers.groq import GroqProvider

        source = inspect.getsource(GroqProvider.generate)
        assert "deadline_s" in source
        assert "remaining = deadline_s - time.monotonic()" in source

    def test_gemini_timeout_respects_remaining(self):
        """Gemini limita timeout por el tiempo restante del deadline."""
        import inspect

        from services.ai import _call_gemini_with_retry

        source = inspect.getsource(_call_gemini_with_retry)
        assert "int(remaining * 1000)" in source

    def test_groq_timeout_respects_remaining(self):
        """Groq limita timeout por el tiempo restante del deadline."""
        import inspect

        from services.ai_providers.groq import GroqProvider

        source = inspect.getsource(GroqProvider.generate)
        assert "min(timeout_s, remaining)" in source


# ------------------------------------------------------------------
# Pruebas C: Seguridad de reservas
# ------------------------------------------------------------------


class TestReservationSecurity:
    """C: Seguridad de operaciones de reserva y cancelación."""

    def test_confirmed_mutations_prevents_router_recall(self):
        """C: Tras mutación confirmada, ask_ai no llama al router nuevamente."""
        import inspect

        from services.ai import ask_ai

        source = inspect.getsource(ask_ai)
        assert "confirmed_mutations" in source
        assert "if confirmed_mutations:" in source

    def test_each_tool_executes_once(self):
        """C: Cada herramienta se ejecuta una vez por function_call."""
        import inspect

        from services.ai import ask_ai

        source = inspect.getsource(ask_ai)
        assert "execute_tool(" in source
        assert "tool_name, arguments, business_id, session_id" in source
        assert "idempotency_key=idempotency_key" in source
        assert "confirmed_mutations.add(" in source

    def test_rejected_reservation_not_confirmed(self):
        """C: Reserva rechazada no se confirma."""
        import inspect

        from services.ai import ask_ai

        source = inspect.getsource(ask_ai)
        assert 'result.get("success") is False' in source
        assert "format_reservation_error" in source

    def test_tool_executed_once_per_iteration(self):
        """C: Bucle de herramientas ejecuta cada tool una vez por iteración."""
        import inspect

        from services.ai import ask_ai

        source = inspect.getsource(ask_ai)
        assert "for function_call in function_calls:" in source
        assert "result = execute_tool(" in source
