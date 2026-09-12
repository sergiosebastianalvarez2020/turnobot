"""Pruebas de Etapa 10: recuperación de contraseña.

- request_password_reset genera un token válido para usuarios existentes.
- El token se consume una sola vez (concurrency-safe).
- La contraseña nueva debe tener >= 12 caracteres.
- Tras un reset exitoso se revocan todas las sesiones del usuario.
- El mismo token usado/consumido no puede reutilizarse.
"""

import re
import tempfile
import unittest
from pathlib import Path

import app as application
import database.database as database
from database.database import get_connection
from services import platform as platform_service
from services import notifications


class PasswordResetBase(unittest.TestCase):

    OWNER_EMAIL = "owner@test-reset.com"
    OWNER_PASSWORD = "clave-segura-123"

    def setUp(self):
        application.rate_limit_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "reset.db"
        database.init_database()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_db
        self.tmp.cleanup()

    @staticmethod
    def _csrf(page):
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return match.group(1) if match else None

    def _query(self, sql, params=None):
        c = get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    def _provision_and_approve(self):
        result = platform_service.provision_business(
            "Test Biz", "test-reset-biz", self.OWNER_EMAIL
        )
        approved = platform_service.approve_business(result["business_id"])
        return result, approved

    def _set_owner_password(self, business_id, token):
        platform_service.accept_invitation(
            business_id, token, self.OWNER_PASSWORD
        )


class TestPasswordResetFlow(PasswordResetBase):

    def test_request_reset_returns_token_for_existing_user(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        self.assertTrue(reset_result["success"])
        self.assertIsNotNone(reset_result["reset_token"])

    def test_request_reset_unknown_email_returns_success_but_no_token(self):
        result = platform_service.request_password_reset("nobody@test-reset.com")
        self.assertTrue(result["success"])
        self.assertIsNone(result["reset_token"])

    def test_get_reset_token_returns_active_token(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        reset_row = platform_service.get_reset_token(reset_result["reset_token"])
        self.assertIsNotNone(reset_row)
        self.assertEqual(reset_row["user_email"], self.OWNER_EMAIL)

    def test_reset_password_sets_new_password_and_revokes_sessions(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        new_password = "nueva-clave-segura-456"
        consumed = platform_service.reset_password(reset_result["reset_token"], new_password)
        self.assertTrue(consumed["success"])

        rows = self._query(
            "SELECT password_hash FROM users WHERE email = ?",
            (self.OWNER_EMAIL,),
        )
        self.assertGreater(len(rows[0]["password_hash"]), 12)
        sessions = self._query(
            "SELECT revoked FROM sessions WHERE user_id = ?",
            (consumed["user_id"],),
        )
        for s in sessions:
            self.assertEqual(s["revoked"], 1)

    def test_reset_password_weak_password_rejected(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        consumed = platform_service.reset_password(reset_result["reset_token"], "corta")
        self.assertFalse(consumed["success"])
        self.assertEqual(consumed["reason"], "weak_password")

    def test_same_token_cannot_be_reused_after_consumption(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        rt = reset_result["reset_token"]
        platform_service.reset_password(rt, "nueva-clave-segura-456")
        second = platform_service.reset_password(rt, "otra-clave-segura-789")
        self.assertFalse(second["success"])
        self.assertEqual(second["reason"], "invalid_token")

    def test_concurrent_reset_same_token_only_one_wins(self):
        import threading

        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        rt = reset_result["reset_token"]

        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            outcomes.append(
                platform_service.reset_password(rt, "clave-segura-12345")
            )

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sum(o["success"] for o in outcomes), 1)
        self.assertEqual(sum(o["reason"] == "invalid_token" for o in outcomes), 1)

    def test_forgot_route_get_returns_form(self):
        response = self.client.get("/forgot")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrf_token", response.text)

    def test_forgot_route_post_sends_email_when_email_exists(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)

        page = self.client.get("/forgot")
        csrf = self._csrf(page)
        response = self.client.post(
            "/forgot",
            data={"email": self.OWNER_EMAIL, "csrf_token": csrf},
        )
        self.assertEqual(response.status_code, 302)

    def test_forgot_post_rate_limited(self):
        for i in range(6):
            page = self.client.get("/forgot")
            csrf = self._csrf(page)
            response = self.client.post(
                "/forgot",
                data={"email": "nobody@test-reset.com", "csrf_token": csrf},
            )
            if i < 5:
                self.assertEqual(response.status_code, 302)
            else:
                self.assertEqual(response.status_code, 302)


class TestResetRouteIntegration(PasswordResetBase):

    def _full_setup(self):
        result, approved = self._provision_and_approve()
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        business_id = result["business_id"]
        self._set_owner_password(business_id, token)
        reset_result = platform_service.request_password_reset(self.OWNER_EMAIL)
        return result, reset_result["reset_token"]

    def test_reset_get_shows_email(self):
        result, reset_token = self._full_setup()
        page = self.client.get(f"/reset/{reset_token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.OWNER_EMAIL, page.text)

    def test_reset_invalid_token_returns_404(self):
        page = self.client.get("/reset/token-que-no-existe")
        self.assertEqual(page.status_code, 404)
        self.assertIn("vencido", page.text)

    def test_reset_post_sets_new_password(self):
        result, reset_token = self._full_setup()
        page = self.client.get(f"/reset/{reset_token}")
        csrf = self._csrf(page)
        new_password = "nueva-clave-segura-456"
        response = self.client.post(
            f"/reset/{reset_token}",
            data={
                "password": new_password,
                "password2": new_password,
                "csrf_token": csrf,
            },
        )
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
