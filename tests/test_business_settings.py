import unittest

import database.database as database
from services.appointments import DEFAULT_TIMEZONE, get_business_timezone


class TestBusinessSettings(unittest.TestCase):
    def test_devuelve_configuracion_del_negocio(self):
        settings = database.get_business_settings(1)

        self.assertEqual(settings["business_name"], "El Corte")
        self.assertEqual(settings["business_type"], "Barbería")
        self.assertEqual(settings["business_initials"], "EC")
        self.assertEqual(settings["business_description"], "Barbería masculina")
        self.assertEqual(settings["timezone"], "America/Argentina/Buenos_Aires")

    def test_timezone_valido(self):
        connection = database.get_connection()
        connection.execute("UPDATE business_settings SET timezone = ? WHERE id = 1", ("UTC",))
        connection.commit()
        connection.close()

        self.assertEqual(get_business_timezone(1), "UTC")

    def test_timezone_invalido_usa_fallback(self):
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET timezone = ? WHERE id = 1", ("Zona/NoExiste",)
        )
        connection.commit()
        connection.close()

        self.assertEqual(get_business_timezone(1), DEFAULT_TIMEZONE)

    def test_frontend_recibe_configuracion_del_negocio(self):
        from app import app

        response = app.test_client().get("/b/el-corte")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<h1 class="landing-title">El Corte</h1>', html)
        self.assertIn('<span class="info-value">America/Argentina/Buenos_Aires</span>', html)
        self.assertIn('data-initials="EC"', html)
        self.assertIn('<span class="landing-badge">BARBERÍA</span>', html)
        self.assertIn("barbería masculina", html.lower())


if __name__ == "__main__":
    unittest.main()
