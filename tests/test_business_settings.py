import tempfile
import unittest
from pathlib import Path

import database.database as database
from services.appointments import DEFAULT_TIMEZONE, get_business_timezone


class TestBusinessSettings(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_devuelve_configuracion_del_negocio(self):
        settings = database.get_business_settings()

        self.assertEqual(settings["business_name"], "El Corte")
        self.assertEqual(settings["business_type"], "Barbería")
        self.assertEqual(settings["business_initials"], "EC")
        self.assertEqual(settings["business_description"], "Barbería masculina")
        self.assertEqual(settings["timezone"], "America/Argentina/Buenos_Aires")

    def test_timezone_valido(self):
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET timezone = ? WHERE id = 1",
            ("UTC",),
        )
        connection.commit()
        connection.close()

        self.assertEqual(get_business_timezone(), "UTC")

    def test_timezone_invalido_usa_fallback(self):
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET timezone = ? WHERE id = 1",
            ("Zona/NoExiste",),
        )
        connection.commit()
        connection.close()

        self.assertEqual(get_business_timezone(), DEFAULT_TIMEZONE)

    def test_frontend_recibe_configuracion_del_negocio(self):
        from app import app

        response = app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-business-name="El Corte"', html)
        self.assertIn('data-timezone="America/Argentina/Buenos_Aires"', html)
        self.assertIn("BARBERÍA", html)
        self.assertIn(">EC<", html)
        self.assertIn("barbería masculina", html.lower())


if __name__ == "__main__":
    unittest.main()
