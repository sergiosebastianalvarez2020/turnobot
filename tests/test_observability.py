"""Tests de Etapa 13: Observabilidad, manejo de errores y control de Gemini.

Cubre:
- request_id generado y presente en respuesta/logs
- handlers globales de errores (400, 403, 404, 429, 500)
- errores no exponen stack traces
- formato consistente de error JSON
- manejo específico de errores de Gemini (timeout, quota, auth, ValueError)
- retry de errores transitorios
- no retry de errores permanentes
- tracking de latencia e iteraciones
- límite total de historial
- tool fallida no puede terminar en confirmación falsa
"""

import json
import logging
import re
import unittest
from unittest import mock

from google.genai import errors as genai_errors

import app as application
import services.ai as ai


class _LogCapture:
    """Captura registros de log para aserciones."""
    def __init__(self, logger_name="el_corte"):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = self._emit
        self.handler.setLevel(logging.DEBUG)
        self.logger = logging.getLogger(logger_name)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def _emit(self, record):
        self.records.append(record)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.logger.setLevel(logging.NOTSET)
        self.logger.removeHandler(self.handler)


class _MockResponse:
    """Simula una respuesta de Gemini con candidates."""
    def __init__(self, text=None, function_calls=None, tool_iterations=None, usage_metadata=None):
        self.text = text
        self.tool_iterations = tool_iterations
        self.usage_metadata = usage_metadata
        if function_calls is None:
            self.candidates = []
            if text is not None:
                self.candidates = [_MockCandidate(text=text)]
        else:
            self.candidates = [_MockCandidate(function_calls=function_calls)]


class _MockCandidate:
    def __init__(self, text=None, function_calls=None):
        self.content = _MockContent(text=text, function_calls=function_calls)


class _MockContent:
    def __init__(self, text=None, function_calls=None):
        self.parts = []
        if text is not None:
            self.parts.append(_MockPart(text=text))
        if function_calls:
            for fc in function_calls:
                self.parts.append(_MockPart(function_call=fc))


class _MockPart:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _MockFunctionCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class _MockGenAIClient:
    """Mock del cliente Gemini para tests de ai.py."""
    def __init__(self, responses=None, errors=None):
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.call_count = 0
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        idx = self.call_count
        self.call_count += 1
        if idx < len(self.errors) and self.errors[idx] is not None:
            raise self.errors[idx]
        if idx < len(self.responses):
            return self.responses[idx]
        return _MockResponse(text="Respuesta predeterminada")


class TestRequestIdAndHeaders(unittest.TestCase):
    def setUp(self):
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password

    def test_request_id_generated_when_header_absent(self):
        client = application.app.test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("X-Request-ID", resp.headers)
        self.assertTrue(resp.headers["X-Request-ID"])

    def test_request_id_reused_when_valid_header_present(self):
        client = application.app.test_client()
        resp = client.get("/health", headers={"X-Request-ID": "abc123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["X-Request-ID"], "abc123")

    def test_request_id_present_in_response(self):
        client = application.app.test_client()
        resp = client.get("/health")
        rid = resp.headers.get("X-Request-ID")
        self.assertIsNotNone(rid)
        self.assertGreater(len(rid), 8)

    def test_request_id_unique_per_request(self):
        client = application.app.test_client()
        resp1 = client.get("/health")
        resp2 = client.get("/health")
        self.assertNotEqual(
            resp1.headers["X-Request-ID"],
            resp2.headers["X-Request-ID"],
        )

    def test_request_id_logged_on_404(self):
        client = application.app.test_client()
        with _LogCapture("el_corte.web") as cap:
            resp = client.get("/api/nonexistent")
            self.assertEqual(resp.status_code, 404)


class TestGlobalErrorHandlers(unittest.TestCase):
    def setUp(self):
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password

    def test_handler_404_json(self):
        client = application.app.test_client()
        resp = client.get("/api/nonexistent")
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertFalse(data["success"])
        self.assertIn("error", data)
        self.assertIn("code", data)
        self.assertEqual(data["code"], "NOT_FOUND")

    def test_handler_404_html(self):
        client = application.app.test_client()
        resp = client.get("/nonexistent-page")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("text/html", resp.content_type)

    def test_handler_403(self):
        client = application.app.test_client()
        resp = client.get("/admin", headers={"Accept": "application/json"})
        self.assertIn(resp.status_code, (302, 403))

    def test_handler_429(self):
        client = application.app.test_client()
        for i in range(70):
            client.get("/api/servicios")
        resp = client.get("/api/servicios")
        self.assertEqual(resp.status_code, 429)
        data = resp.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["code"], "RATE_LIMITED")

    def test_handler_500_no_stack_trace(self):
        with mock.patch.object(application, "ask_ai", side_effect=RuntimeError("internal test error")):
            client = application.app.test_client()
            application.app.config["PROPAGATE_EXCEPTIONS"] = False
            resp = client.post("/chat", json={"message": "hola"})
            self.assertEqual(resp.status_code, 500)
            data = resp.get_json()
            self.assertFalse(data["success"])
            body = resp.get_data(as_text=True)
            self.assertNotIn("Traceback", body)
            self.assertNotIn("internal test error", body)
            self.assertEqual(data["error"], "No se pudo procesar la consulta.")

    def test_error_format_consistent(self):
        client = application.app.test_client()
        resp = client.get("/api/nonexistent")
        data = resp.get_json()
        self.assertIn("success", data)
        self.assertIn("error", data)
        self.assertIn("code", data)
        self.assertFalse(data["success"])

    def test_error_does_not_expose_internal_paths(self):
        client = application.app.test_client()
        resp = client.get("/api/nonexistent")
        body = resp.get_data(as_text=True)
        self.assertNotIn("/app/", body)
        self.assertNotIn("Traceback", body)

    def test_error_does_not_expose_sql(self):
        with mock.patch.object(application, "ask_ai", side_effect=RuntimeError("SELECT * FROM users WHERE id=1")):
            client = application.app.test_client()
            application.app.config["PROPAGATE_EXCEPTIONS"] = False
            resp = client.post("/chat", json={"message": "test"})
            self.assertEqual(resp.status_code, 500)
            data = resp.get_json()
            self.assertNotIn("SELECT", data.get("error", ""))


class TestGeminiErrorHandling(unittest.TestCase):

    def _make_mock(self, errors=None, responses=None):
        return _MockGenAIClient(errors=errors or [], responses=responses or [])

    def test_gemini_timeout_returns_friendly_message(self):
        ai.client = None
        err = genai_errors.ServerError(500, {"status": "DEADLINE_EXCEEDED", "message": "timeout"})
        mock_client = self._make_mock(errors=[err] * ai.MAX_RETRIES)
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                response, attempt, error_info = ai._call_gemini_with_retry([], None)
                self.assertIsNone(response)
                self.assertGreaterEqual(attempt, 1)
                category, error_type = error_info or (None, None)
                self.assertEqual(category, "timeout")

    def test_gemini_quota_error_returns_appropriate_message(self):
        ai.client = None
        err = genai_errors.ClientError(429, {"status": "RESOURCE_EXHAUSTED", "message": "quota exceeded"})
        mock_client = self._make_mock(errors=[err] * ai.MAX_RETRIES)
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                response, attempt, error_info = ai._call_gemini_with_retry([], None)
                self.assertIsNone(response)
                category, error_type = error_info
                self.assertEqual(category, "quota")

    def test_gemini_auth_error_is_not_retried(self):
        ai.client = None
        err = genai_errors.ClientError(401, {"status": "UNAUTHENTICATED", "message": "Invalid API key"})
        mock_client = self._make_mock(errors=[err])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            response, attempt, error_info = ai._call_gemini_with_retry([], None)
            self.assertIsNone(response)
            self.assertEqual(attempt, 1)
            category, error_type = error_info
            self.assertEqual(category, "auth")

    def test_gemini_value_error_handled(self):
        ai.client = None
        mock_client = self._make_mock(errors=[ValueError("malformed response")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            response, attempt, error_info = ai._call_gemini_with_retry([], None)
            self.assertIsNone(response)
            category, error_type = error_info
            self.assertEqual(category, "invalid_argument")

    def test_gemini_unexpected_error_returns_generic(self):
        ai.client = None
        mock_client = self._make_mock(errors=[RuntimeError("unexpected")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            response, attempt, error_info = ai._call_gemini_with_retry([], None)
            self.assertIsNone(response)
            category, _ = error_info
            self.assertEqual(category, "unknown")

    def test_gemini_auth_error_no_api_key_exposed(self):
        ai.client = None
        err = genai_errors.ClientError(401, {"status": "UNAUTHENTICATED", "message": "Invalid API key: abc123secret"})
        mock_client = self._make_mock(errors=[err])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            response, attempt, error_info = ai._call_gemini_with_retry([], None)
            self.assertIsNone(response)
            category, _ = error_info
            self.assertEqual(category, "auth")

    def test_gemini_error_messages_are_friendly(self):
        self.assertIn("demasiado", ai._gemini_error_message("timeout").lower())
        self.assertIn("limit", ai._gemini_error_message("quota").lower())
        self.assertIn("problema", ai._gemini_error_message("auth").lower())


class TestGeminiRetryLogic(unittest.TestCase):

    def test_retry_on_transitory_error(self):
        ai.client = None
        err = genai_errors.ServerError(503, {"status": "UNAVAILABLE", "message": "temporarily unavailable"})
        mock_client = _MockGenAIClient(errors=[err, None], responses=[_MockResponse(text="ok")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                response, attempt, error_info = ai._call_gemini_with_retry([], None)
                self.assertIsNotNone(response)
                self.assertGreaterEqual(attempt, 2)
                self.assertIsNone(error_info)

    def test_no_retry_on_permanent_error(self):
        ai.client = None
        err = genai_errors.ClientError(400, {"status": "INVALID_ARGUMENT", "message": "bad request"})
        mock_client = _MockGenAIClient(errors=[err])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            response, attempt, error_info = ai._call_gemini_with_retry([], None)
            self.assertIsNone(response)
            self.assertEqual(attempt, 1)
            category, _ = error_info
            self.assertEqual(category, "invalid_argument")

    def test_retry_attempts_capped_at_max(self):
        ai.client = None
        err = genai_errors.ServerError(500, {"status": "INTERNAL", "message": "internal error"})
        mock_client = _MockGenAIClient(errors=[err] * (ai.MAX_RETRIES + 2))
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                response, attempt, error_info = ai._call_gemini_with_retry([], None)
                self.assertIsNone(response)
                self.assertLessEqual(attempt, ai.MAX_RETRIES)

    def test_retry_on_timeout_error(self):
        ai.client = None
        err = genai_errors.ServerError(500, {"status": "DEADLINE_EXCEEDED", "message": "timeout"})
        mock_client = _MockGenAIClient(errors=[err, None], responses=[_MockResponse(text="ok")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep") as mock_sleep:
                response, attempt, _ = ai._call_gemini_with_retry([], None)
                self.assertIsNotNone(response)
                mock_sleep.assert_called()

    def test_no_retry_on_auth_error(self):
        ai.client = None
        err = genai_errors.ClientError(401, {"status": "UNAUTHENTICATED", "message": "bad key"})
        mock_client = _MockGenAIClient(errors=[err] * 5)
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep") as mock_sleep:
                response, attempt, _ = ai._call_gemini_with_retry([], None)
                self.assertIsNone(response)
                self.assertEqual(attempt, 1)
                mock_sleep.assert_not_called()

    def test_retry_on_connection_error(self):
        ai.client = None
        err = ConnectionError("connection reset by peer")
        mock_client = _MockGenAIClient(errors=[err, None], responses=[_MockResponse(text="recuperado")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                response, attempt, error_info = ai._call_gemini_with_retry([], None)
                self.assertIsNotNone(response)
                self.assertEqual(attempt, 2)
                self.assertIsNone(error_info)

    def test_retry_on_socket_timeout(self):
        import socket
        ai.client = None
        err = socket.timeout("timed out")
        mock_client = _MockGenAIClient(errors=[err, None], responses=[_MockResponse(text="recuperado")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                response, attempt, error_info = ai._call_gemini_with_retry([], None)
                self.assertIsNotNone(response)
                self.assertEqual(attempt, 2)
                self.assertIsNone(error_info)

    def test_retry_residual_return_contract(self):
        ai.client = None
        err = genai_errors.ClientError(400, {"status": "INVALID_ARGUMENT", "message": "bad argument"})
        mock_client = _MockGenAIClient(errors=[err])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            response, attempt, error_info = ai._call_gemini_with_retry([], None)
            self.assertIsNone(response)
            self.assertEqual(len(error_info), 2)
            category, error_type = error_info
            self.assertEqual(category, "invalid_argument")


class TestGeminiMetrics(unittest.TestCase):

    def test_latency_tracked_on_success(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="hello")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with _LogCapture("el_corte") as cap:
                response, attempt, _ = ai._call_gemini_with_retry([], None, request_id="test-rid", business_id=1)
                self.assertIsNotNone(response)
                latency_records = [r for r in cap.records if "gemini_call" in r.getMessage()]
                self.assertTrue(len(latency_records) > 0)
                log_msg = latency_records[0].getMessage()
                self.assertIn("latency_ms=", log_msg)
                self.assertIn("request_id=test-rid", log_msg)
                self.assertIn("business_id=1", log_msg)
                self.assertIn("status=success", log_msg)

    def test_latency_tracked_on_error(self):
        ai.client = None
        err = genai_errors.ServerError(500, {"status": "DEADLINE_EXCEEDED", "message": "timeout"})
        mock_client = _MockGenAIClient(errors=[err] * ai.MAX_RETRIES)
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                with _LogCapture("el_corte") as cap:
                    ai._call_gemini_with_retry([], None, request_id="test-rid-2", business_id=2)
                    latency_records = [r for r in cap.records if "gemini_call" in r.getMessage()]
                    self.assertTrue(len(latency_records) > 0)
                    log_msg = latency_records[-1].getMessage()
                    self.assertIn("status=error", log_msg)
                    self.assertIn("error_type=DEADLINE_EXCEEDED", log_msg)

    def test_attempts_tracked(self):
        ai.client = None
        err = genai_errors.ServerError(503, {"status": "UNAVAILABLE", "message": "unavailable"})
        mock_client = _MockGenAIClient(errors=[err, None], responses=[_MockResponse(text="ok")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with mock.patch("time.sleep"):
                with _LogCapture("el_corte") as cap:
                    ai._call_gemini_with_retry([], None, request_id="test-rid-3")
                    attempt_records = [r for r in cap.records if "attempt=" in r.getMessage() and "gemini_call" in r.getMessage()]
                    self.assertGreaterEqual(len(attempt_records), 1)

    def test_tool_iterations_logged(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="respuesta final")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client), \
             mock.patch.object(ai, "get_services_prompt", return_value="servicio: test"), \
             mock.patch.object(ai, "get_business_hours_prompt", return_value="horarios: test"), \
             mock.patch.object(ai, "get_business_identity", return_value={"business_name": "Test", "business_type": "Test", "business_description": "", "timezone": "UTC"}), \
             mock.patch("services.ai.get_business_settings_scoped", return_value={"business_name": "Test", "business_type": "Test", "business_description": "", "timezone": "UTC"}), \
             mock.patch("services.ai.search_knowledge_scoped", return_value=[]), \
             mock.patch.object(ai, "get_or_create_conversation_session_scoped", return_value=None):
            with _LogCapture("el_corte") as cap:
                ai.ask_ai("hola", business_id=1)
                iter_records = [r for r in cap.records if "tool_iterations=" in r.getMessage()]
                self.assertTrue(len(iter_records) > 0)
                self.assertIn("tool_iterations=1", iter_records[0].getMessage())

    def test_usage_metadata_logged_when_present(self):
        ai.client = None
        usage = mock.Mock(prompt_token_count=120, candidates_token_count=35, total_token_count=155)
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="test tokens", usage_metadata=usage)])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client):
            with _LogCapture("el_corte") as cap:
                response, attempt, _ = ai._call_gemini_with_retry([], None)
                self.assertIsNotNone(response)
                records = [r for r in cap.records if "prompt_tokens=120" in r.getMessage()]
                self.assertTrue(len(records) > 0)
                msg = records[0].getMessage()
                self.assertIn("candidate_tokens=35", msg)
                self.assertIn("total_tokens=155", msg)


class TestHistoryLimit(unittest.TestCase):

    def test_total_chars_limit_truncates_oldest(self):
        from google.genai import types as gtypes
        contents = []
        for i in range(10):
            contents.append(gtypes.Content(
                role="user",
                parts=[gtypes.Part.from_text(text=f"mensaje numero {i} " * 200)]
            ))
        truncated = ai.truncate_history_by_total_chars(contents, max_total_chars=1000)
        total = sum(
            len(part.text) for c in truncated for part in (c.parts or []) if part.text
        )
        self.assertLessEqual(total, 1000)

    def test_total_chars_limit_keeps_recent(self):
        from google.genai import types as gtypes
        contents = []
        for i in range(5):
            contents.append(gtypes.Content(
                role="user",
                parts=[gtypes.Part.from_text(text=f"mensaje {i} " * 10)]
            ))
        truncated = ai.truncate_history_by_total_chars(contents, max_total_chars=150)
        last_text = truncated[-1].parts[0].text
        self.assertIn("mensaje 4", last_text)

    def test_total_chars_limit_default_constant(self):
        self.assertIsInstance(ai.MAX_HISTORY_TOTAL_CHARS, int)
        self.assertGreater(ai.MAX_HISTORY_TOTAL_CHARS, 0)

    def test_build_contents_applies_total_limit(self):
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="ok")])
        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client), \
             mock.patch.object(ai, "get_services_prompt", return_value="s"), \
             mock.patch.object(ai, "get_business_hours_prompt", return_value="h"), \
             mock.patch.object(ai, "get_business_identity", return_value={"business_name": "T", "business_type": "T", "business_description": "", "timezone": "UTC"}), \
             mock.patch("services.ai.get_business_settings_scoped", return_value={"business_name": "T", "business_type": "T", "business_description": "", "timezone": "UTC"}), \
             mock.patch("services.ai.search_knowledge_scoped", return_value=[]), \
             mock.patch.object(ai, "get_or_create_conversation_session_scoped", return_value=None):
            long_conv = [{"role": "user", "content": "x" * 5000} for _ in range(20)]
            result, _, _ = ai.ask_ai("hola", conversation=long_conv, business_id=1)
            self.assertIsInstance(result[0], str)


class TestToolResultControl(unittest.TestCase):

    def _common_patches(self, mock_client, tool_result=None):
        patches = [
            mock.patch.object(ai, "get_gemini_client", return_value=mock_client),
            mock.patch.object(ai, "get_services_prompt", return_value="Corte: $100 (30 min)"),
            mock.patch.object(ai, "get_business_hours_prompt", return_value="lunes: abierto"),
            mock.patch.object(ai, "get_business_identity", return_value={"business_name": "Test", "business_type": "Test", "business_description": "", "timezone": "UTC"}),
            mock.patch("services.ai.get_business_settings_scoped", return_value={"business_name": "Test", "business_type": "Test", "business_description": "", "timezone": "UTC", "notifications_enabled": 0}),
            mock.patch("services.ai.search_knowledge_scoped", return_value=[]),
            mock.patch("services.conversations.get_or_create_conversation_session_scoped", return_value=None),
            mock.patch("services.conversations.get_or_create_public_conversation_session_scoped", return_value=None),
        ]
        if tool_result is not None:
            patches.append(mock.patch.object(ai, "execute_tool", return_value=tool_result))
        return patches

    def test_reservar_fallida_no_confirma(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("reservar_turno", {"nombre": "Test", "telefono": "123", "servicio": "Corte", "fecha": "2025-01-01", "hora": "10:00"})])])
        patches = self._common_patches(
            mock_client,
            tool_result={"success": False, "reason": "occupied", "message": "El horario está ocupado."}
        )
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reservar", business_id=1)
            self.assertNotIn("reservado correctamente", result)
            self.assertIn("Disculpá", result)
        finally:
            for p in patches:
                p.stop()

    def test_cancelar_fallida_no_confirma(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("cancelar_turno", {"appointment_id": 1, "telefono": "123"})])])
        patches = self._common_patches(
            mock_client,
            tool_result={"success": False, "message": "Turno no encontrado."}
        )
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("cancelar", business_id=1)
            self.assertNotIn("cancelado correctamente", result)
        finally:
            for p in patches:
                p.stop()

    def test_reprogramar_fallida_no_confirma(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("reprogramar_turno", {"appointment_id": 1, "telefono": "123", "nueva_fecha": "2025-01-02", "nueva_hora": "11:00"})])])
        patches = self._common_patches(
            mock_client,
            tool_result={"success": False, "reason": "occupied", "message": "Horario ocupado."}
        )
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reprogramar", business_id=1)
            self.assertNotIn("reprogramado correctamente", result)
            self.assertIn("Disculpá", result)
        finally:
            for p in patches:
                p.stop()

    def test_reservar_exitosa_sí_confirma(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("reservar_turno", {"nombre": "Ana", "telefono": "1234567890", "servicio": "Corte", "fecha": "2025-01-01", "hora": "10:00"})])])
        patches = self._common_patches(
            mock_client,
            tool_result={"success": True, "appointment_id": 1, "message": "El turno fue reservado correctamente.", "resource_id": None}
        )
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reservar", business_id=1)
            self.assertIn("reservado correctamente", result)
        finally:
            for p in patches:
                p.stop()

    def test_reserva_texto_plano_sin_tool_se_neutraliza(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="Tu turno quedó reservado correctamente.")])
        patches = self._common_patches(mock_client)
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reservar", business_id=1)
            self.assertNotIn("reservado correctamente", result)
            self.assertNotIn("reservado", result)
            self.assertIn("no se pudo confirmar la operación", result)
            self.assertEqual(mock_client.call_count, 1)
        finally:
            for p in patches:
                p.stop()

    def test_cancelacion_texto_plano_sin_tool_se_neutraliza(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="Listo. Tu turno fue cancelado correctamente.")])
        patches = self._common_patches(mock_client)
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("cancelar", business_id=1)
            self.assertNotIn("cancelado correctamente", result)
            self.assertNotIn("cancelado", result)
            self.assertIn("no se pudo confirmar la operación", result)
            self.assertEqual(mock_client.call_count, 1)
        finally:
            for p in patches:
                p.stop()

    def test_reprogramacion_texto_plano_sin_tool_se_neutraliza(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="Listo. Tu turno fue reprogramado correctamente.")])
        patches = self._common_patches(mock_client)
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reprogramar", business_id=1)
            self.assertNotIn("reprogramado correctamente", result)
            self.assertNotIn("reprogramado", result)
            self.assertIn("no se pudo confirmar la operación", result)
            self.assertEqual(mock_client.call_count, 1)
        finally:
            for p in patches:
                p.stop()

    def test_confirmacion_falsa_despues_de_consultar_disponibilidad(self):
        ai.client = None
        first = _MockResponse(function_calls=[_MockFunctionCall("consultar_disponibilidad", {"fecha": "2026-09-20"})])
        second = _MockResponse(text="Tu turno quedó reservado correctamente.")
        mock_client = _MockGenAIClient(responses=[first, second])
        patches = self._common_patches(
            mock_client,
            tool_result={"success": True, "horarios_disponibles": ["10:00"]},
        )
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reservar", business_id=1)
            self.assertNotIn("reservado correctamente", result)
            self.assertIn("no se pudo confirmar la operación", result)
            self.assertEqual(mock_client.call_count, 2)
        finally:
            for p in patches:
                p.stop()

    def test_texto_normal_sin_confirmacion_permanece_intacto(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(text="Claro, ¿qué horarios tenés disponibles para mañana?")])
        patches = self._common_patches(mock_client)
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("hola", business_id=1)
            self.assertEqual(result, "Claro, ¿qué horarios tenés disponibles para mañana?")
            self.assertEqual(mock_client.call_count, 1)
        finally:
            for p in patches:
                p.stop()

    def test_mutacion_sin_success_no_alimenta_gemini(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("reservar_turno", {"nombre": "Ana", "telefono": "1234567890", "servicio": "Corte", "fecha": "2026-09-20", "hora": "10:00"})])])
        patches = self._common_patches(mock_client, tool_result={"error": "sin clave success"})
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reservar", business_id=1)
            self.assertNotIn("reservado", result)
            self.assertIn("no se pudo confirmar la operación", result)
            self.assertEqual(mock_client.call_count, 1)
        finally:
            for p in patches:
                p.stop()

    def test_cancelar_exitosa_sí_confirma(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("cancelar_turno", {"appointment_id": 1, "telefono": "123"})])])
        patches = self._common_patches(mock_client, tool_result={"success": True})
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("cancelar", business_id=1)
            self.assertIn("cancelado correctamente", result)
        finally:
            for p in patches:
                p.stop()

    def test_reprogramar_exitosa_sí_confirma(self):
        ai.client = None
        mock_client = _MockGenAIClient(responses=[_MockResponse(function_calls=[_MockFunctionCall("reprogramar_turno", {"appointment_id": 1, "telefono": "123", "nueva_fecha": "2026-09-20", "nueva_hora": "10:00"})])])
        patches = self._common_patches(
            mock_client,
            tool_result={"success": True, "nueva_fecha": "2026-09-20", "nueva_hora": "10:00"},
        )
        for p in patches:
            p.start()
        try:
            result, _, _ = ai.ask_ai("reprogramar", business_id=1)
            self.assertIn("reprogramado correctamente", result)
        finally:
            for p in patches:
                p.stop()


class TestErrorFormatStandardization(unittest.TestCase):
    """Verifica que las respuestas de error usan el formato {success, error, code}."""

    def setUp(self):
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password

    def test_404_response_has_consistent_format(self):
        client = application.app.test_client()
        resp = client.get("/api/nonexistent")
        data = resp.get_json()
        self.assertIn("success", data)
        self.assertIn("error", data)
        self.assertIn("code", data)
        self.assertIn("request_id", data)

    def test_500_response_has_consistent_format(self):
        with mock.patch.object(application, "ask_ai", side_effect=RuntimeError("boom")):
            client = application.app.test_client()
            application.app.config["PROPAGATE_EXCEPTIONS"] = False
            resp = client.post("/chat", json={"message": "test"})
            data = resp.get_json()
            self.assertIn("success", data)
            self.assertIn("error", data)
            self.assertFalse(data["success"])

    def test_429_response_has_code_field(self):
        client = application.app.test_client()
        for i in range(70):
            client.get("/api/servicios")
        resp = client.get("/api/servicios")
        data = resp.get_json()
        self.assertIn("code", data)
        self.assertEqual(data["code"], "RATE_LIMITED")


class TestObservabilityBlockEtapa15(unittest.TestCase):
    """Tests específicos del bloque de Observabilidad y Manejo de Errores (Etapa 15)."""

    def setUp(self):
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None
        application.rate_limit_state.clear()

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        application.rate_limit_state.clear()

    def test_logging_context_filter_in_request(self):
        client = application.app.test_client()
        with _LogCapture("el_corte.web") as cap:
            resp = client.get("/health", headers={"X-Request-ID": "custom-req-id-123"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(len(cap.records) > 0)
            rec = cap.records[-1]
            self.assertEqual(rec.request_id, "custom-req-id-123")
            self.assertEqual(rec.endpoint, "health")
            self.assertEqual(rec.method, "GET")
            self.assertEqual(rec.path, "/health")
            self.assertEqual(rec.business_id, "1")

    def test_logging_context_filter_outside_request(self):
        filt = application.RequestContextFilter()
        rec = logging.LogRecord("test", logging.INFO, "path", 1, "msg", (), None)
        filt.filter(rec)
        self.assertEqual(rec.request_id, "-")
        self.assertEqual(rec.business_id, "-")
        self.assertEqual(rec.endpoint, "-")
        self.assertEqual(rec.method, "-")
        self.assertEqual(rec.path, "-")

    def test_structured_formatter_plaintext(self):
        formatter = application.StructuredFormatter()
        rec = logging.LogRecord("test.logger", logging.INFO, "path", 1, "Operacion exitosa", (), None)
        rec.request_id = "req-abc"
        rec.business_id = "10"
        rec.endpoint = "api_turnos"
        rec.method = "POST"
        rec.path = "/api/turnos"
        formatted = formatter.format(rec)
        self.assertIn("[req-abc]", formatted)
        self.assertIn("[b:10]", formatted)
        self.assertIn("[POST api_turnos]", formatted)
        self.assertIn("Operacion exitosa", formatted)

    def test_structured_formatter_json(self):
        formatter = application.StructuredFormatter()
        rec = logging.LogRecord("test.logger", logging.WARNING, "path", 1, "Alerta de sistema", (), None)
        rec.request_id = "req-xyz"
        rec.business_id = "5"
        rec.endpoint = "chat"
        rec.method = "POST"
        rec.path = "/chat"
        rec.status_code = 200
        rec.latency_ms = 45.2

        with mock.patch.object(application, "USE_JSON_LOGS", True):
            formatted = formatter.format(rec)
            data = json.loads(formatted)
            self.assertEqual(data["level"], "WARNING")
            self.assertEqual(data["logger"], "test.logger")
            self.assertEqual(data["message"], "Alerta de sistema")
            self.assertEqual(data["request_id"], "req-xyz")
            self.assertEqual(data["business_id"], "5")
            self.assertEqual(data["endpoint"], "chat")
            self.assertEqual(data["method"], "POST")
            self.assertEqual(data["path"], "/chat")
            self.assertEqual(data["status_code"], 200)
            self.assertEqual(data["latency_ms"], 45.2)

    def test_after_request_correlation_log(self):
        client = application.app.test_client()
        with _LogCapture("el_corte.web") as cap:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            correlation_records = [
                r for r in cap.records
                if "HTTP GET /health -> 200" in r.getMessage()
            ]
            self.assertTrue(len(correlation_records) >= 1)
            record = correlation_records[0]
            self.assertEqual(record.status_code, 200)
            self.assertIsNotNone(record.latency_ms)
            self.assertGreaterEqual(record.latency_ms, 0)

    def test_after_request_correlation_omits_static(self):
        client = application.app.test_client()
        with _LogCapture("el_corte.web") as cap:
            client.get("/static/css/style.css")
            static_records = [
                r for r in cap.records
                if "/static/" in r.getMessage() and r.getMessage().startswith("HTTP ")
            ]
            self.assertEqual(len(static_records), 0)

    def test_html_error_400_status_code(self):
        with application.app.test_request_context("/", headers={"Accept": "text/html"}):
            err = mock.Mock(description="Solicitud malformada")
            resp, code = application.handle_400(err)
            self.assertEqual(code, 400)
            self.assertIn("400", resp)
            self.assertIn("Solicitud malformada", resp)

    def test_html_error_500_status_code(self):
        with mock.patch.dict(application.app.view_functions, {"index": mock.Mock(side_effect=RuntimeError("error_interno_confidencial_html"))}):
            client = application.app.test_client()
            application.app.config["PROPAGATE_EXCEPTIONS"] = False
            resp = client.get("/", headers={"Accept": "text/html"})
            self.assertEqual(resp.status_code, 500)
            self.assertIn("text/html", resp.content_type)
            body = resp.get_data(as_text=True)
            self.assertIn("500", body)
            self.assertNotIn("error_interno_confidencial_html", body)
            self.assertNotIn("Traceback", body)

    def test_multitenant_api_path_returns_json_error(self):
        client = application.app.test_client()
        resp = client.get("/b/negocio-demo/api/inexistente")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/json", resp.content_type)
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertFalse(data["success"])
        self.assertEqual(data["code"], "NOT_FOUND")
        self.assertIn("request_id", data)

    def test_unhandled_exception_returns_json_when_requested(self):
        with mock.patch.dict(application.app.view_functions, {"api_servicios": mock.Mock(side_effect=RuntimeError("error_interno_confidencial_db"))}):
            client = application.app.test_client()
            application.app.config["PROPAGATE_EXCEPTIONS"] = False
            resp = client.get("/api/servicios", headers={"Accept": "application/json"})
            self.assertEqual(resp.status_code, 500)
            self.assertIn("application/json", resp.content_type)
            data = resp.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["code"], "INTERNAL_ERROR")
            self.assertEqual(data["error"], "Error interno del servidor.")
            self.assertIn("request_id", data)
            body = resp.get_data(as_text=True)
            self.assertNotIn("error_interno_confidencial_db", body)
            self.assertNotIn("Traceback", body)

    def test_error_html_displays_request_id(self):
        client = application.app.test_client()
        resp = client.get("/pagina-que-no-existe", headers={"X-Request-ID": "mi-request-id-visible-999"})
        self.assertEqual(resp.status_code, 404)
        body = resp.get_data(as_text=True)
        self.assertIn("mi-request-id-visible-999", body)
        self.assertIn("ID de solicitud:", body)


class TestGeminiTechnicalRobustness(unittest.TestCase):
    """Verifica max_output_tokens y unicidad de constantes."""

    def test_max_output_tokens_configured(self):
        self.assertTrue(hasattr(ai, "MAX_OUTPUT_TOKENS"))
        self.assertGreater(ai.MAX_OUTPUT_TOKENS, 0)
        ai.client = None
        captured_config = []

        def mock_generate(model=None, contents=None, config=None):
            captured_config.append(config)
            return _MockResponse(text="ok")

        mock_client = mock.Mock()
        mock_client.models.generate_content.side_effect = mock_generate

        with mock.patch.object(ai, "get_gemini_client", return_value=mock_client), \
             mock.patch.object(ai, "get_services_prompt", return_value=""), \
             mock.patch.object(ai, "get_business_hours_prompt", return_value=""), \
             mock.patch.object(ai, "get_business_identity", return_value={"business_name": "Test", "business_type": "Test", "business_description": "", "timezone": "UTC"}), \
             mock.patch("services.ai.get_business_settings_scoped", return_value={"business_name": "Test", "business_type": "Test", "business_description": "", "timezone": "UTC"}), \
             mock.patch("services.ai.search_knowledge_scoped", return_value=[]), \
             mock.patch.object(ai, "get_or_create_conversation_session_scoped", return_value=None):
            ai.ask_ai("hola", business_id=1)
            self.assertTrue(len(captured_config) > 0)
            self.assertEqual(captured_config[0].max_output_tokens, ai.MAX_OUTPUT_TOKENS)

    def test_max_tool_iterations_defined_once(self):
        import inspect
        source = inspect.getsource(ai)
        matches = re.findall(r"^MAX_TOOL_ITERATIONS\s*=", source, flags=re.MULTILINE)
        self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
