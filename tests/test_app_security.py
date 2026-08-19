import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from services import appointments


class TestAdminSecurity(unittest.TestCase):
    def setUp(self):
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
        response = self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.location)


class TestDomainValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_servicio_inactivo_o_inexistente(self):
        date = (datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date() + timedelta(days=1)).isoformat()
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Servicio inexistente", date, "09:00"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_service")

    def test_telefono_con_formato_se_normaliza(self):
        date = (datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date() + timedelta(days=1)).isoformat()
        result = appointments.create_appointment(
            "Ana Pérez", "+54 383-843-9222", "Corte", date, "10:00"
        )
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
