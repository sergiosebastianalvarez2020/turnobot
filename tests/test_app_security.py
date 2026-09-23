import re
import unittest

from werkzeug.security import generate_password_hash

import app as application


class TestAdminSecurity(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.client = application.app.test_client()
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password

    def test_admin_requiere_sesion(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_login_requiere_csrf(self):
        response = self.client.post("/login", data={"password": "correcta"})
        self.assertEqual(response.status_code, 400)

    def test_login_con_hash_y_csrf(self):
        login_page = self.client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        response = self.client.post("/login", data={"password": "correcta", "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.location)

    def test_post_administrativo_sin_csrf_no_modifica_estado(self):
        login_page = self.client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        login = self.client.post("/login", data={"password": "correcta", "csrf_token": token})
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
        login_page = self.client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        response = self.client.post("/login", data={"password": "correcta", "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        # Verificar la cookie de sesión en el redirect
        set_cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("SameSite=Strict", set_cookie)
