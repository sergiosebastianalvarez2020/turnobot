"""Pruebas de Fase 2: ejecución real de herramientas de IA y respuestas malformadas.

Cubre:
- `consultar_recursos`, `consultar_disponibilidad` y `buscar_turnos_cliente`
  ejecutados contra DB real a través de `execute_tool` (sin mockear el dispatch).
- aislamiento por negocio de las herramientas.
- contrato de `execute_tool` (cen contexto, herramienta desconocida).
- respuestas malformadas de Gemini (sin candidates / sin texto) -> mensaje seguro.
- rama `ask_ai` que formatea "no encontramos turnos" cuando la búsqueda
  de turnos del cliente devuelve vacío.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import app as application
import database.database as database
import services.ai as ai
from services import appointments
from services.ai import execute_tool


def next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class BaseAIToolsTest(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.business_id = 1
        self.valid_date = next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()


class TestConsultarRecursosTool(BaseAIToolsTest):
    def test_devuelve_recursos_activos_del_negocio(self):
        database.create_resource_scoped(1, "Cancha 1")
        database.create_resource_scoped(1, "Cancha 2")

        result = execute_tool("consultar_recursos", {}, 1)

        self.assertTrue(result["success"])
        nombres = {r["nombre"] for r in result["recursos"]}
        self.assertIn("Cancha 1", nombres)
        self.assertIn("Cancha 2", nombres)

    def test_omite_recursos_inactivos(self):
        database.create_resource_scoped(1, "Cancha Activa", active=True)
        database.create_resource_scoped(1, "Cancha Inactiva", active=False)

        result = execute_tool("consultar_recursos", {}, 1)

        self.assertTrue(result["success"])
        nombres = {r["nombre"] for r in result["recursos"]}
        self.assertIn("Cancha Activa", nombres)
        self.assertNotIn("Cancha Inactiva", nombres)

    def test_sin_recursos_devuelve_lista_vacia(self):
        result = execute_tool("consultar_recursos", {}, 1)
        self.assertTrue(result["success"])
        self.assertEqual(result["recursos"], [])


class TestConsultarDisponibilidadTool(BaseAIToolsTest):
    def test_devuelve_horarios_disponibles_del_dia(self):
        result = execute_tool(
            "consultar_disponibilidad",
            {"fecha": self.valid_date, "servicio": "Corte"},
            self.business_id,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["fecha"], self.valid_date)
        self.assertTrue(result["horarios_disponibles"])

    def test_reserva_confirmada_se_refleja_en_disponibilidad(self):
        disponibles = execute_tool(
            "consultar_disponibilidad",
            {"fecha": self.valid_date, "servicio": "Corte"},
            self.business_id,
        )["horarios_disponibles"]
        self.assertTrue(disponibles)
        hora = disponibles[0]

        resultado = appointments.create_appointment(
            "Cliente", "3838439222", "Corte", self.valid_date, hora, business_id=1
        )
        self.assertTrue(resultado["success"])

        result = execute_tool(
            "consultar_disponibilidad",
            {"fecha": self.valid_date, "servicio": "Corte"},
            self.business_id,
        )
        self.assertTrue(result["success"])
        self.assertNotIn(hora, result["horarios_disponibles"])

    def test_fecha_invalida_devuelve_lista_vacia(self):
        result = execute_tool(
            "consultar_disponibilidad",
            {"fecha": "no-es-una-fecha", "servicio": "Corte"},
            self.business_id,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["horarios_disponibles"], [])


class TestBuscarTurnosClienteTool(BaseAIToolsTest):
    def test_devuelve_turnos_del_cliente(self):
        appointments.create_appointment(
            "Cliente", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )

        result = execute_tool(
            "buscar_turnos_cliente", {"nombre": "Cliente", "telefono": "3838439222"}, 1
        )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["turnos"]), 1)
        self.assertEqual(result["turnos"][0]["customer_name"], "Cliente")

    def test_sin_turnos_devuelve_lista_vacia(self):
        result = execute_tool(
            "buscar_turnos_cliente", {"nombre": "Sin Turno", "telefono": "3838439000"}, 1
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["turnos"], [])

    def test_no_expone_turnos_de_otro_negocio(self):
        appointments.create_appointment(
            "Cliente", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )

        result = execute_tool(
            "buscar_turnos_cliente", {"nombre": "Cliente", "telefono": "3838439222"}, 2
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["turnos"], [])


class TestToolDispatchContract(BaseAIToolsTest):
    def test_sin_negocio_devuelve_error_de_contexto(self):
        result = execute_tool("consultar_recursos", {}, None)
        self.assertEqual(result["success"], False)
        self.assertEqual(result["error"], "Contexto de negocio inválido.")

    def test_herramienta_desconocida_devuelve_none(self):
        self.assertIsNone(execute_tool("herramienta_inventada", {}, self.business_id))


# ============================================================
# Respuestas malformadas de Gemini (vía ask_ai, observable)
# ============================================================


class _NoTextResponse:
    """Simula una respuesta de Gemini sin candidates ni texto."""

    def __init__(self):
        self.candidates = []

    @property
    def text(self):
        raise ValueError("La respuesta de Gemini no contiene texto extraíble.")


class _ToolCallResponse:
    def __init__(self, tool_name, args):
        fc = mock.Mock(name=tool_name)
        fc.name = tool_name
        fc.args = args
        part = mock.Mock()
        part.function_call = fc
        part.text = None
        content = mock.Mock()
        content.parts = [part]
        candidate = mock.Mock()
        candidate.content = content
        self.candidates = [candidate]


class _FakeGenAIClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.call_count = 0
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        idx = self.call_count
        self.call_count += 1
        if idx < len(self.responses):
            return self.responses[idx]
        return _NoTextResponse()


class TestGeminiMalformedResponses(BaseAIToolsTest):
    def _run_ask_ai(self, mock_client):
        ai.client = None
        patches = [
            mock.patch.object(ai, "get_gemini_client", return_value=mock_client),
            mock.patch.object(ai, "get_services_prompt", return_value="Corte: $100 (30 min)"),
            mock.patch.object(ai, "get_business_hours_prompt", return_value="lunes: abierto"),
            mock.patch(
                "services.ai.get_business_settings_scoped",
                return_value={
                    "business_name": "El Corte",
                    "business_type": "Barbería",
                    "business_description": "",
                    "timezone": "America/Argentina/Buenos_Aires",
                },
            ),
            mock.patch("services.ai.search_knowledge_scoped", return_value=[]),
            mock.patch(
                "services.conversations.get_or_create_public_conversation_session_scoped",
                return_value=None,
            ),
        ]
        for p in patches:
            p.start()
        try:
            return ai.ask_ai("hola", business_id=self.business_id)
        finally:
            for p in patches:
                p.stop()

    def test_respuesta_sin_candidates_devuelve_mensaje_seguro(self):
        result, _, _ = self._run_ask_ai(_FakeGenAIClient(responses=[_NoTextResponse()]))
        self.assertEqual(result, "Disculpá, no pude generar una respuesta.")

    def test_respuesta_sin_respuestas_disponibles_devuelve_mensaje_seguro(self):
        result, _, _ = self._run_ask_ai(_FakeGenAIClient(responses=[]))
        self.assertEqual(result, "Disculpá, no pude generar una respuesta.")

    def test_buscar_turnos_sin_resultados_formatea_no_encontramos(self):
        mock_client = _FakeGenAIClient(
            responses=[
                _ToolCallResponse(
                    "buscar_turnos_cliente", {"nombre": "Nadie", "telefono": "3838439000"}
                )
            ]
        )
        result, _, _ = self._run_ask_ai(mock_client)
        self.assertIn("No encontramos turnos con esos datos", result)


if __name__ == "__main__":
    unittest.main()
