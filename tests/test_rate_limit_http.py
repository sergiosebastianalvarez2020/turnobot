"""Comportamiento observable del rate limiting por HTTP: bloqueo, headers y reset.

Cubre la ventana (RATE_LIMIT_WINDOW_SECONDS) de forma observable: tras agotar
el límite por HTTP se obtiene 429 (o el código RATE_LIMITED en la API), y al
avanzar la ventana (a través del helper de producción ``_prune_rate_limit_state``
con ``now`` explícito, igual que test_rate_limit_memory) el mismo request vuelve
a estar permitido.
"""

import re
import tempfile
import time
import unittest
from pathlib import Path

import app as application
import database.database as database
from extensions import RATE_LIMIT_WINDOW_SECONDS


class _RateLimitHTTPBase(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True
        self._tmp = tempfile.TemporaryDirectory()
        self._original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self._tmp.name) / "rl.db"
        database.init_database()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self._original_database_path
        self._tmp.cleanup()

    def _advance_window(self):
        """Simula el paso de la ventana expirando las entradas de rate limiting."""
        application._prune_rate_limit_state(now=time.monotonic() + RATE_LIMIT_WINDOW_SECONDS + 1)


class TestPublicAPIRateLimit(_RateLimitHTTPBase):
    ENDPOINT = "/b/el-corte/api/servicios"

    def test_api_is_blocked_after_limit_and_allowed_after_window(self):
        for _ in range(application.API_REQUEST_LIMIT):
            response = self.client.get(self.ENDPOINT)
            self.assertEqual(response.status_code, 200)

        blocked = self.client.get(self.ENDPOINT)
        self.assertEqual(blocked.status_code, 429)
        data = blocked.get_json()
        self.assertFalse(data["success"])
        self.assertEqual(data["code"], "RATE_LIMITED")

        self._advance_window()
        after_reset = self.client.get(self.ENDPOINT)
        self.assertEqual(after_reset.status_code, 200)

    def test_api_rate_limit_is_scoped_per_business(self):
        connection = database.get_connection()
        connection.execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Padel Club', 'padel-club')"
        )
        connection.commit()
        connection.close()

        for _ in range(application.API_REQUEST_LIMIT):
            self.client.get(self.ENDPOINT)

        blocked = self.client.get(self.ENDPOINT)
        self.assertEqual(blocked.status_code, 429)

        other_business = self.client.get("/b/padel-club/api/servicios")
        self.assertEqual(other_business.status_code, 200)


class TestLoginRateLimit(_RateLimitHTTPBase):
    def _post_login(self, password="incorrecta"):
        login_page = self.client.get("/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        self.assertIsNotNone(match)
        return self.client.post("/login", data={"password": password, "csrf_token": match.group(1)})

    def test_login_blocked_after_ten_attempts_and_reset(self):
        for _ in range(10):
            response = self._post_login()
            self.assertEqual(response.status_code, 200)

        blocked = self._post_login()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Demasiados intentos", blocked.get_data(as_text=True))

        self._advance_window()
        after_reset = self._post_login()
        self.assertEqual(after_reset.status_code, 200)


class TestWizardRateLimit(_RateLimitHTTPBase):
    ENDPOINT = "/b/el-corte/reservar/confirmar"

    def _csrf(self):
        page = self.client.get("/b/el-corte/reservar")
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        if match is None:
            return None
        return match.group(1)

    def _post_confirmar(self):
        return self.client.post(
            self.ENDPOINT,
            json={
                "csrf_token": self._csrf(),
                "nombre": "Juan Perez",
                "telefono": "12345678",
                "email": "juan@test.com",
                "servicio": "Corte",
                "fecha": "2025-12-31",
                "hora": "10:00",
            },
        )

    def test_wizard_429_with_retry_after_window_and_reset(self):
        for _ in range(application.API_REQUEST_LIMIT):
            response = self._post_confirmar()
            self.assertNotEqual(response.status_code, 429)

        blocked = self._post_confirmar()
        self.assertEqual(blocked.status_code, 429)
        data = blocked.get_json()
        self.assertEqual(data["code"], "RATE_LIMITED")
        self.assertEqual(blocked.headers.get("Retry-After"), str(RATE_LIMIT_WINDOW_SECONDS))

        self._advance_window()
        after_reset = self._post_confirmar()
        self.assertNotEqual(after_reset.status_code, 429)


if __name__ == "__main__":
    unittest.main()
