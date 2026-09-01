import re
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database


class TestAdminBusinessSettings(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self.client = application.app.test_client()
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

        login_page = self.client.get("/login")
        self.csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            login_page.text,
        ).group(1)
        response = self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": self.csrf_token},
        )
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_administrador_puede_guardar_configuracion_valida(self):
        response = self.client.post(
            "/admin/configuracion",
            data={
                "csrf_token": self.csrf_token,
                "business_name": "Studio Bella",
                "business_type": "Estudio de belleza",
                "business_initials": "SB",
                "business_description": "Atención personalizada",
                "timezone": "UTC",
            },
        )

        self.assertEqual(response.status_code, 302)
        settings = database.get_business_settings()
        self.assertEqual(settings["business_name"], "Studio Bella")
        self.assertEqual(settings["business_type"], "Estudio de belleza")
        self.assertEqual(settings["business_initials"], "SB")
        self.assertEqual(settings["business_description"], "Atención personalizada")
        self.assertEqual(settings["timezone"], "UTC")

    def test_timezone_invalido_no_se_guarda(self):
        before = dict(database.get_business_settings())

        response = self.client.post(
            "/admin/configuracion",
            data={
                "csrf_token": self.csrf_token,
                "business_name": "Cambio inválido",
                "business_type": "Negocio",
                "business_initials": "CI",
                "business_description": "No debe guardarse",
                "timezone": "Zona/NoExiste",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(dict(database.get_business_settings()), before)

    def test_campos_obligatorios_invalidos_no_se_guardan(self):
        before = dict(database.get_business_settings())

        response = self.client.post(
            "/admin/configuracion",
            data={
                "csrf_token": self.csrf_token,
                "business_name": "",
                "business_type": "",
                "business_initials": "",
                "business_description": "No debe guardarse",
                "timezone": "UTC",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(dict(database.get_business_settings()), before)


if __name__ == "__main__":
    unittest.main()
