"""Tests de regresión: redacción de tokens sensibles en logs.

Verifica que tokens en paths sensibles (password reset, invitaciones,
gestión de turnos, conversaciones) nunca aparecen en los logs de la aplicación.
"""

import json
import logging
import unittest

import app as application
from application.logging_config import StructuredFormatter, redact_sensitive_path

_REAL_TOKEN = "aB3x-def456-GHI789-jkl012-mnP987-qRsTuVwXyZ-0xABCDEF"


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


class TestRedactSensitivePath(unittest.TestCase):
    """Tests unitarios de la función redact_sensitive_path."""

    def test_password_reset_token_redacted(self):
        result = redact_sensitive_path("/reset/SECRETO_REAL")
        self.assertEqual(result, "/reset/[REDACTED]")
        self.assertNotIn("SECRETO_REAL", result)

    def test_public_invitation_token_redacted(self):
        result = redact_sensitive_path("/b/demo/invitacion/SECRETO_REAL")
        self.assertEqual(result, "/b/demo/invitacion/[REDACTED]")
        self.assertNotIn("SECRETO_REAL", result)

    def test_staff_invitation_token_redacted(self):
        result = redact_sensitive_path("/b/demo/invitacion-staff/SECRETO_REAL")
        self.assertEqual(result, "/b/demo/invitacion-staff/[REDACTED]")
        self.assertNotIn("SECRETO_REAL", result)

    def test_appointment_management_token_redacted(self):
        result = redact_sensitive_path("/b/demo/turno/SECRETO_REAL")
        self.assertEqual(result, "/b/demo/turno/[REDACTED]")
        self.assertNotIn("SECRETO_REAL", result)

    def test_conversation_token_redacted(self):
        result = redact_sensitive_path("/api/conversations/SECRETO_REAL/messages")
        self.assertEqual(result, "/api/conversations/[REDACTED]/messages")
        self.assertNotIn("SECRETO_REAL", result)

    def test_normal_route_preserved(self):
        self.assertEqual(redact_sensitive_path("/health"), "/health")
        self.assertEqual(redact_sensitive_path("/api/servicios"), "/api/servicios")
        self.assertEqual(redact_sensitive_path("/b/el-corte"), "/b/el-corte")
        self.assertEqual(redact_sensitive_path("/b/el-corte/admin"), "/b/el-corte/admin")

    def test_complex_alphanumeric_token(self):
        token = "aB3xK9mP2vQ8nR4sT6wZ7yJ1hF5gH3iL0bU6cY8dE2fG9"
        result = redact_sensitive_path(f"/reset/{token}")
        self.assertEqual(result, "/reset/[REDACTED]")
        self.assertNotIn(token, result)

    def test_complex_token_with_hyphens(self):
        token = "abc123-def456-ghi789-jkl012"
        result = redact_sensitive_path(f"/reset/{token}")
        self.assertEqual(result, "/reset/[REDACTED]")
        self.assertNotIn(token, result)

    def test_long_token(self):
        token = "aB3x-def456-GHI789-jkl012-mnP987-qRsTuVwXyZ-0xABCDEF" * 4
        result = redact_sensitive_path(f"/b/demo/turno/{token}")
        self.assertEqual(result, "/b/demo/turno/[REDACTED]")
        self.assertNotIn(token, result)

    def test_no_partial_token_leakage(self):
        token = "abcdef123456-secret-token-xyz789"
        for route in (
            f"/reset/{token}",
            f"/b/demo/invitacion/{token}",
            f"/b/demo/invitacion-staff/{token}",
            f"/b/demo/turno/{token}",
            f"/api/conversations/{token}/messages",
        ):
            result = redact_sensitive_path(route)
            self.assertNotIn("abcdef123456", result)
            self.assertNotIn("secret-token", result)
            self.assertIn("[REDACTED]", result)

    def test_non_string_input(self):
        self.assertEqual(redact_sensitive_path(None), "-")
        self.assertEqual(redact_sensitive_path(123), "-")
        self.assertEqual(redact_sensitive_path(""), "")


class TestTokenLoggingEndToEnd(unittest.TestCase):
    """Tests end-to-end: el token no aparece en los logs de la aplicacio'n."""

    def setUp(self):
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password

    def _capture_logs_for_request(self, path):
        cap = _LogCapture("el_corte.web")
        cap.__enter__()
        try:
            client = application.app.test_client()
            client.get(path)
        finally:
            cap.__exit__()
        return cap.records

    def test_reset_token_absent_from_all_logs(self):
        records = self._capture_logs_for_request(f"/reset/{_REAL_TOKEN}")
        self.assertTrue(len(records) > 0)
        for rec in records:
            self.assertNotIn(_REAL_TOKEN, rec.getMessage())
            self.assertNotIn(_REAL_TOKEN, rec.path)

    def test_reset_token_redacted_in_http_log(self):
        records = self._capture_logs_for_request(f"/reset/{_REAL_TOKEN}")
        http_records = [
            r
            for r in records
            if r.getMessage().startswith("HTTP") and "/reset/" in r.getMessage()
        ]
        self.assertTrue(len(http_records) > 0)
        for rec in http_records:
            self.assertIn("/reset/[REDACTED]", rec.getMessage())
            self.assertNotIn(_REAL_TOKEN, rec.getMessage())

    def test_invitation_token_absent_from_all_logs(self):
        records = self._capture_logs_for_request(f"/b/el-corte/invitacion/{_REAL_TOKEN}")
        for rec in records:
            self.assertNotIn(_REAL_TOKEN, rec.getMessage())
            self.assertNotIn(_REAL_TOKEN, rec.path)

    def test_staff_invitation_token_absent_from_all_logs(self):
        records = self._capture_logs_for_request(
            f"/b/el-corte/invitacion-staff/{_REAL_TOKEN}"
        )
        for rec in records:
            self.assertNotIn(_REAL_TOKEN, rec.getMessage())
            self.assertNotIn(_REAL_TOKEN, rec.path)

    def test_appointment_token_absent_from_all_logs(self):
        records = self._capture_logs_for_request(f"/b/el-corte/turno/{_REAL_TOKEN}")
        for rec in records:
            self.assertNotIn(_REAL_TOKEN, rec.getMessage())
            self.assertNotIn(_REAL_TOKEN, rec.path)

    def test_normal_route_path_preserved_in_http_log(self):
        records = self._capture_logs_for_request("/health")
        http_records = [
            r for r in records if r.getMessage().startswith("HTTP")
        ]
        self.assertTrue(len(http_records) > 0)
        for rec in http_records:
            if "/health" in rec.getMessage():
                self.assertIn("/health", rec.getMessage())

    def test_token_not_in_json_formatted_output(self):
        with _LogCapture("el_corte.web") as cap:
            client = application.app.test_client()
            client.get(f"/reset/{_REAL_TOKEN}")
        for rec in cap.records:
            formatted_data = {
                "message": rec.getMessage(),
                "path": getattr(rec, "path", "-"),
                "endpoint": getattr(rec, "endpoint", "-"),
                "method": getattr(rec, "method", "-"),
                "level": rec.levelname,
                "logger": rec.name,
            }
            json_output = json.dumps(formatted_data)
            self.assertNotIn(_REAL_TOKEN, json_output)

    def test_full_log_record_serialization_no_token(self):
        with _LogCapture("el_corte.web") as cap:
            client = application.app.test_client()
            client.get(f"/reset/{_REAL_TOKEN}")
        for rec in cap.records:
            self.assertNotIn(_REAL_TOKEN, rec.getMessage())
            self.assertNotIn(_REAL_TOKEN, str(rec.path))
            self.assertNotIn(_REAL_TOKEN, rec.endpoint)
            self.assertNotIn(_REAL_TOKEN, rec.method)
            self.assertNotIn(_REAL_TOKEN, rec.name)

    def test_structured_formatter_json_output_no_token(self):
        formatter = StructuredFormatter()
        with _LogCapture("el_corte.web") as cap:
            client = application.app.test_client()
            client.get(f"/reset/{_REAL_TOKEN}")
        for rec in cap.records:
            formatted = formatter.format(rec)
            self.assertNotIn(_REAL_TOKEN, formatted)


if __name__ == "__main__":
    unittest.main()
