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
        from services.ai import MODEL
        from google.genai import types as gtypes
        import services.ai as ai
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


if __name__ == "__main__":
    unittest.main()
