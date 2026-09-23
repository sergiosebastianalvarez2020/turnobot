"""Pruebas de Etapa 11: configuración del modelo de IA."""

import os
import unittest
from importlib import reload
from unittest import mock


class TestAIModelConfiguration(unittest.TestCase):
    def setUp(self):
        self._original = os.environ.pop("AI_MODEL", None)

    def tearDown(self):
        if self._original is not None:
            os.environ["AI_MODEL"] = self._original
        elif "AI_MODEL" in os.environ:
            del os.environ["AI_MODEL"]

    def test_default_model_es_valido(self):

        import services.ai as ai
        from services.ai import MODEL

        self.assertEqual(ai.MODEL, "gemini-2.5-flash")
        self.assertIn("gemini", MODEL)

    def test_model_overridable_via_env(self):
        with mock.patch.dict("os.environ", {"AI_MODEL": "gemini-2.0-flash"}):
            import services.ai as ai

            reload(ai)
            self.assertEqual(ai.MODEL, "gemini-2.0-flash")

    def test_model_not_empty(self):
        from services.ai import MODEL

        self.assertTrue(MODEL)
        self.assertGreater(len(MODEL), 5)


class TestAIResourcePrompts(unittest.TestCase):
    """Regresión: prompts de contexto no deben lanzar NameError."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        import app as application
        import database.database as database

        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        application.rate_limit_state.clear()

    def tearDown(self):
        import database.database as database

        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_get_resources_prompt_sin_recursos_devuelve_string(self):
        from services.ai import get_resources_prompt

        result = get_resources_prompt(1)
        self.assertIsInstance(result, str)
        self.assertEqual(result, "No hay recursos reservables en este momento.")

    def test_get_resources_prompt_con_recursos_lista_nombres(self):
        from database.database import create_resource_scoped
        from services.ai import get_resources_prompt

        create_resource_scoped(1, "Cancha 1")
        result = get_resources_prompt(1)
        self.assertIsInstance(result, str)
        self.assertIn("Cancha 1", result)
        self.assertIn("ID:", result)

    def test_get_services_prompt_devuelve_string(self):
        from services.ai import get_services_prompt

        result = get_services_prompt(1)
        self.assertIsInstance(result, str)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
