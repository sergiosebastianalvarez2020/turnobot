import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from werkzeug.security import generate_password_hash

import app as application
from tests._pg_compat import PostgreSQLTestCase


class TestAdminSecurity(unittest.TestCase, PostgreSQLTestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        # El login ya NO acepta ADMIN_PASSWORD_HASH: el owner se provisiona de
        # forma explícita. El contexto se pusha para que el provisionamiento use
        # el pool de ESTE test y no el respaldo global compartido.
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.addCleanup(self._app_context.pop)

        from database.seed_auth import provision_owner_from_bootstrap

        self._owner_email = "owner@test.local"
        self._owner_password = "correcta"
        provision_owner_from_bootstrap(
            1, self._owner_email, generate_password_hash(self._owner_password)
        )

        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password

    def _login(self):
        """Inicia sesión con el owner provisionado explícitamente."""
        login_page = self.client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        return self.client.post(
            "/login",
            data={
                "email": self._owner_email,
                "password": self._owner_password,
                "csrf_token": token,
            },
        )

    def test_admin_requiere_sesion(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_login_requiere_csrf(self):
        response = self.client.post("/login", data={"password": "correcta"})
        self.assertEqual(response.status_code, 400)

    def test_login_de_owner_explicito_con_csrf(self):
        """Login válido (owner provisionado explícitamente) + CSRF -> 302 a /admin."""
        response = self._login()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.location)

    def test_login_no_permite_tomar_owner_con_hash_de_bootstrap(self):
        application.ADMIN_PASSWORD_HASH = generate_password_hash("bootstrap-password")
        page = self.client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login",
            data={
                "email": self._owner_email,
                "password": "bootstrap-password",
                "csrf_token": csrf,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Credenciales inválidas.", response.text)

    def test_login_reemplaza_estado_de_sesion_preexistente(self):
        with self.client.session_transaction() as current_session:
            current_session["session_token"] = "attacker-controlled-session"
            current_session["csrf_token"] = "attacker-controlled-csrf"

        response = self.client.post(
            "/login",
            data={
                "email": self._owner_email,
                "password": self._owner_password,
                "csrf_token": "attacker-controlled-csrf",
            },
        )
        self.assertEqual(response.status_code, 302)
        from database.database import get_user_by_email_scoped

        owner = get_user_by_email_scoped(self._owner_email)
        with self.client.session_transaction() as current_session:
            self.assertEqual(current_session["user_id"], owner["id"])
            self.assertNotEqual(current_session["session_token"], "attacker-controlled-session")
            self.assertNotEqual(current_session["csrf_token"], "attacker-controlled-csrf")

    def test_login_no_distingue_usuario_password_ni_membership(self):
        from database.database import create_user_scoped

        absent_membership_user = create_user_scoped(
            "sin-membresia@test.local", generate_password_hash("una-clave-valida"), active=True
        )
        self.assertIsNotNone(absent_membership_user)
        cases = (
            ("desconocido@test.local", "incorrecta"),
            (self._owner_email, "incorrecta"),
            ("sin-membresia@test.local", "una-clave-valida"),
        )
        responses = []
        for email, password in cases:
            page = self.client.get("/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            response = self.client.post(
                "/login", data={"email": email, "password": password, "csrf_token": csrf}
            )
            responses.append((response.status_code, response.text))
        self.assertEqual(len({status for status, _ in responses}), 1)
        self.assertEqual(len({body for _, body in responses}), 1)
        self.assertIn("Credenciales inválidas.", responses[0][1])

    def test_reset_link_ignora_host_controlado_por_el_cliente(self):
        page = self.client.get("/forgot", headers={"Host": "evil.example"})
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        with (
            patch(
                "routes.auth_public.platform_service.request_password_reset",
                return_value={"reset_token": "reset-secret"},
            ),
            patch("routes.auth_public.notifications.send_password_reset_email") as send_email,
        ):
            response = self.client.post(
                "/forgot",
                data={"csrf_token": csrf, "email": "owner@example.test"},
                headers={"Host": "evil.example"},
            )
        self.assertEqual(response.status_code, 302)
        sent_link = send_email.call_args.args[1]
        self.assertTrue(sent_link.startswith(self.app.config["PUBLIC_BASE_URL"] + "/reset/"))
        self.assertNotIn("evil.example", sent_link)

    def test_reset_link_ignora_otro_host_valido_enviado_por_el_cliente(self):
        client = self.app.test_client()
        host = "another-valid.example"
        page = client.get("/forgot", headers={"Host": host})
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        with (
            patch(
                "routes.auth_public.platform_service.request_password_reset",
                return_value={"reset_token": "reset-secret"},
            ),
            patch("routes.auth_public.notifications.send_password_reset_email") as send_email,
        ):
            response = client.post(
                "/forgot",
                data={"csrf_token": csrf, "email": "owner@example.test"},
                headers={"Host": host},
            )

        self.assertEqual(response.status_code, 302)
        sent_link = send_email.call_args.args[1]
        public_origin = urlsplit(self.app.config["PUBLIC_BASE_URL"])
        link = urlsplit(sent_link)
        self.assertEqual((link.scheme, link.netloc), (public_origin.scheme, public_origin.netloc))
        self.assertEqual(link.path, "/reset/reset-secret")
        self.assertNotIn(host, sent_link)

    def test_api_publica_no_entrega_saldo_solo_por_conocer_telefono(self):
        from routes.public_api import _get_public_points_response

        with (
            patch.object(application, "is_api_request_allowed", return_value=True),
            patch(
                "routes.public_api.ensure_loyalty_settings_scoped",
                return_value={"enabled": True, "points_per_completed_appointment": 5},
            ),
            patch("routes.public_api.loyalty.get_account") as get_account,
            application.app.test_request_context("/api/puntos?phone=5551234567"),
        ):
            response = _get_public_points_response(1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["balance"], 0)
        self.assertTrue(response.get_json()["verification_required"])
        get_account.assert_not_called()

    def test_resolve_business_fallback_respects_suspension(self):
        from database.database import get_connection

        connection = get_connection()
        try:
            connection.execute("UPDATE businesses SET active = TRUE WHERE id = 1")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(application.resolve_business()["id"], 1)

        connection = get_connection()
        try:
            connection.execute("UPDATE businesses SET active = FALSE WHERE id = 1")
            connection.commit()
        finally:
            connection.close()
        self.addCleanup(self._restore_business_one_active)
        self.assertIsNone(application.resolve_business())

    @staticmethod
    def _restore_business_one_active():
        from database.database import get_connection

        connection = get_connection()
        try:
            connection.execute("UPDATE businesses SET active = TRUE WHERE id = 1")
            connection.commit()
        finally:
            connection.close()

    def test_post_administrativo_sin_csrf_no_modifica_estado(self):
        login = self._login()
        self.assertEqual(login.status_code, 302)
        response = self.client.post(
            "/admin/configuracion",
            data={
                "business_name": "atacante",
                "business_type": "x",
                "business_initials": "X",
                "timezone": "UTC",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_csp_header_present(self):
        """Verifica que el header Content-Security-Policy esté presente y no vacío."""
        response = self.client.get("/login")
        csp = response.headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertNotEqual(csp.strip(), "")
        # Verificar directivas fundamentales
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src", csp)
        self.assertIn("style-src", csp)
        self.assertIn("font-src", csp)
        self.assertIn("img-src", csp)
        self.assertIn("connect-src", csp)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)

    def test_session_cookie_samesite_strict(self):
        """Verifica que la cookie de sesión use SameSite=Strict."""
        response = self._login()
        self.assertEqual(response.status_code, 302)
        # Verificar la cookie de sesión en el redirect
        set_cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("SameSite=Strict", set_cookie)
