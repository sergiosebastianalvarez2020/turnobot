import sqlite3
import unittest
from unittest.mock import patch

import app as application


class TestBusinessContext(unittest.TestCase):
    def test_request_normal_resuelve_el_corte(self):
        with application.app.test_request_context("/"):
            application.load_current_business()
            self.assertEqual(application.get_current_business_id(), 1)

    def test_contexto_no_persiste_entre_requests(self):
        with application.app.test_request_context("/"):
            application.load_current_business()
            self.assertEqual(application.get_current_business_id(), 1)
        with application.app.test_request_context("/"):
            self.assertIsNone(application.get_current_business_id())

    def test_slug_existente_resuelve_id_1(self):
        business = application.resolve_business("el-corte")
        self.assertEqual(business["id"], 1)

    def test_slug_inexistente_devuelve_resultado_controlado(self):
        self.assertIsNone(application.resolve_business("no-existe"))

    def test_root_sigue_funcionando_y_mantiene_el_corte(self):
        with application.app.test_client() as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
        with application.app.test_request_context("/"):
            application.load_current_business()
            self.assertEqual(application.get_current_business_id(), 1)
            self.assertEqual(application.g.current_business["slug"], "el-corte")

    def test_slug_route_resuelve_el_corte(self):
        with application.app.test_client() as client:
            response = client.get("/b/el-corte")
            self.assertEqual(response.status_code, 200)

        with application.app.test_request_context("/b/el-corte"):
            application.load_current_business()
            self.assertEqual(application.get_current_business_id(), 1)
            self.assertEqual(application.g.current_business["slug"], "el-corte")

    def test_slug_inexistente_devuelve_404_y_no_fallback_al_corte(self):
        with application.app.test_client() as client:
            response = client.get("/b/slug-inexistente")
            self.assertEqual(response.status_code, 404)

        with application.app.test_request_context("/b/slug-inexistente"):
            with self.assertRaises(Exception):
                application.load_current_business()
            self.assertFalse(hasattr(application.g, "current_business"))

    def test_slug_existente_de_negocio_b_usa_su_config_publica(self):
        business_b = {"id": 2, "name": "Business B", "slug": "business-b"}
        settings_b = {
            "business_name": "Business B",
            "business_type": "Barbería",
            "business_initials": "BB",
            "business_description": "Negocio de prueba",
            "timezone": "America/Argentina/Buenos_Aires",
            "slot_duration": 60,
            "break_between_slots": 0,
        }

        with patch.object(application, "resolve_business", return_value=business_b):
            with patch.object(application, "get_business_settings_scoped", return_value=settings_b):
                with application.app.test_client() as client:
                    response = client.get("/b/business-b")
                    self.assertEqual(response.status_code, 200)
                    body = response.get_data(as_text=True)
                    self.assertIn("Business B", body)
                    self.assertIn("Negocio de prueba", body)
                    self.assertIn("BB", body)
                    self.assertNotIn("El Corte", body)

    def test_dos_requests_no_comparten_estado(self):
        second_business = {"id": 2, "name": "Business B", "slug": "business-b"}
        with patch.object(application, "resolve_business", side_effect=[
            {"id": 1, "name": "El Corte", "slug": "el-corte"},
            second_business,
        ]):
            with application.app.test_request_context("/"):
                application.load_current_business()
                self.assertEqual(application.get_current_business_id(), 1)
            with application.app.test_request_context("/"):
                application.load_current_business()
                self.assertEqual(application.get_current_business_id(), 2)

    def test_no_hay_tenant_actual_en_variable_global_mutable(self):
        self.assertFalse(hasattr(application, "current_business"))
        self.assertFalse(hasattr(application, "current_business_id"))


if __name__ == "__main__":
    unittest.main()
