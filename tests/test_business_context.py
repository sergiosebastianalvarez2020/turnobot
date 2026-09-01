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
