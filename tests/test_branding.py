"""Pruebas de Etapa 10: validación de branding.

- Colores primario/secundario deben ser HEX válidos (#RRGGBB).
- Logo debe ser URL válida (http/https) sin espacios.
- Valores vacíos son aceptados (clear branding).
- Branding válido se persiste correctamente.
"""

import re
import tempfile
import unittest
from pathlib import Path

import app as application
import database.database as database
from database.database import get_connection


class BrandingBase(unittest.TestCase):
    OWNER_EMAIL = "owner@test-branding.com"
    OWNER_PASSWORD = "clave-segura-123"

    def setUp(self):
        application.rate_limit_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "branding.db"
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

    def _setup(self):
        from services import platform as platform_service

        result = platform_service.provision_business(
            "Branding Biz", "test-branding-biz", self.OWNER_EMAIL
        )
        approved = platform_service.approve_business(result["business_id"])
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        platform_service.accept_invitation(result["business_id"], token, self.OWNER_PASSWORD)

        page = self.client.get("/b/test-branding-biz/login")
        csrf = self._csrf(page)
        self.client.post(
            "/b/test-branding-biz/login",
            data={"email": self.OWNER_EMAIL, "password": self.OWNER_PASSWORD, "csrf_token": csrf},
        )

    def _post_config(self, **overrides):
        page = self.client.get("/b/test-branding-biz/admin")
        csrf = self._csrf(page)
        data = {
            "business_name": "Branding Biz",
            "business_type": "Barbería",
            "business_initials": "BB",
            "business_description": "Test",
            "timezone": "America/Argentina/Buenos_Aires",
            "notifications_enabled": "1",
            "slot_duration": "60",
            "break_between_slots": "0",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "csrf_token": csrf,
        }
        data.update(overrides)
        return self.client.post("/b/test-branding-biz/admin/configuracion", data=data)

    def _business_id(self):
        row = self._query("SELECT id FROM businesses WHERE slug = 'test-branding-biz'")
        return row[0]["id"] if row else None


class TestBrandingValidation(BrandingBase):
    def setUp(self):
        super().setUp()
        self._setup()

    def test_invalid_primary_color_rejected(self):
        response = self._post_config(primary_color="rojo")
        self.assertEqual(response.status_code, 302)
        self.assertIn("HEX", response.headers.get("Location", ""))

    def test_invalid_secondary_color_rejected(self):
        response = self._post_config(secondary_color="blue")
        self.assertEqual(response.status_code, 302)
        self.assertIn("HEX", response.headers.get("Location", ""))

    def test_short_hex_color_rejected(self):
        response = self._post_config(primary_color="#fff")
        self.assertEqual(response.status_code, 302)
        self.assertIn("HEX", response.headers.get("Location", ""))

    def test_valid_hex_colors_accepted(self):
        response = self._post_config(primary_color="#FF5733", secondary_color="#123456")
        self.assertEqual(response.status_code, 302)

        rows = self._query(
            "SELECT logo_url, primary_color, secondary_color FROM business_settings WHERE business_id = ?",
            (self._business_id(),),
        )
        self.assertEqual(rows[0]["primary_color"], "#FF5733")
        self.assertEqual(rows[0]["secondary_color"], "#123456")

    def test_invalid_logo_url_rejected(self):
        response = self._post_config(logo_url="ftp://example.com/logo.png")
        self.assertEqual(response.status_code, 302)
        self.assertIn("URL+v%C3%A1lida", response.headers.get("Location", ""))

    def test_logo_url_with_spaces_rejected(self):
        response = self._post_config(logo_url="https://example.com/logo .png")
        self.assertEqual(response.status_code, 302)
        self.assertIn("espacios", response.headers.get("Location", ""))

    def test_valid_logo_url_accepted(self):
        response = self._post_config(
            logo_url="https://example.com/logo.png",
            primary_color="#FF5733",
            secondary_color="#123456",
        )
        self.assertEqual(response.status_code, 302)

        rows = self._query(
            "SELECT logo_url FROM business_settings WHERE business_id = ?", (self._business_id(),)
        )
        self.assertEqual(rows[0]["logo_url"], "https://example.com/logo.png")

    def test_empty_branding_accepted(self):
        response = self._post_config()
        self.assertEqual(response.status_code, 302)

        rows = self._query(
            "SELECT logo_url, primary_color, secondary_color FROM business_settings WHERE business_id = ?",
            (self._business_id(),),
        )
        self.assertEqual(rows[0]["logo_url"], "")
        self.assertEqual(rows[0]["primary_color"], "")
        self.assertEqual(rows[0]["secondary_color"], "")


if __name__ == "__main__":
    unittest.main()
