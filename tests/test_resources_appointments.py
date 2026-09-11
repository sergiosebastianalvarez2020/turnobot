"""Etapa 1: recursos reservables — comportamiento de reservas.

Cubre:
- Negocio sin recursos mantiene el comportamiento anterior.
- Dos recursos distintos pueden reservarse simultáneamente.
- El mismo recurso no admite dos reservas solapadas.
- Una reserva global (resource_id NULL) bloquea los recursos.
- Una reserva de recurso no bloquea otro recurso.
- Disponibilidad por recurso (get_available_times).
- Cancelación y reprogramación conservan el resource_id.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import database.database as database
from database.database import get_connection
from services.appointments import (
    cancel_appointment,
    create_appointment,
    get_available_times,
    reschedule_appointment_admin,
)


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class BaseResourceBookingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._original_database_path = database.DATABASE_PATH

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.date = _next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _create(self, business_id=1, time="09:00", resource_id=None,
                service="Corte", name="Cliente Test"):
        return create_appointment(
            name, "111111111", service, self.date, time, business_id,
            resource_id=resource_id,
        )

    @staticmethod
    def _query(sql, params=None):
        connection = get_connection()
        try:
            return connection.execute(sql, params or ()).fetchall()
        finally:
            connection.close()


class TestBusinessWithoutResources(BaseResourceBookingTest):
    """Un negocio sin recursos mantiene el comportamiento anterior."""

    def test_reserva_sin_recurso_funciona_igual(self):
        result = self._create()
        self.assertTrue(result["success"])
        self.assertIsNone(result["resource_id"])

    def test_doble_reserva_mismo_slot_sigue_bloqueada(self):
        self._create(time="09:00")
        result = self._create(time="09:00", name="Otro Cliente")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")

    def test_slots_sin_recurso_consideran_todos_los_turnos(self):
        self._create(time="09:00")
        slots = get_available_times(self.date, 1, service="Corte")
        self.assertNotIn("09:00", slots)
        self.assertIn("10:00", slots)


class TestBusinessWithResources(BaseResourceBookingTest):

    def setUp(self):
        super().setUp()
        self.court1 = database.create_resource_scoped(1, "Cancha 1")
        self.court2 = database.create_resource_scoped(1, "Cancha 2")

    def test_dos_recursos_distintos_mismo_horario(self):
        r1 = self._create(resource_id=self.court1)
        r2 = self._create(resource_id=self.court2, name="Otro Cliente")
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])

    def test_mismo_recurso_no_admite_solapamiento(self):
        # "Corte" dura 30 min: dos reservas a la misma hora solapan en el
        # mismo recurso.
        r1 = self._create(resource_id=self.court1, time="09:00")
        r2 = self._create(resource_id=self.court1, time="09:00",
                          name="Otro Cliente")
        self.assertTrue(r1["success"])
        self.assertFalse(r2["success"])
        self.assertEqual(r2["reason"], "occupied")

    def test_reserva_global_bloquea_los_recursos(self):
        r_global = self._create(resource_id=None, time="09:00")
        r_recurso = self._create(resource_id=self.court1, time="09:00",
                                 name="Otro Cliente")
        self.assertTrue(r_global["success"])
        self.assertFalse(r_recurso["success"])
        self.assertEqual(r_recurso["reason"], "occupied")

    def test_reserva_de_recurso_ocupa_la_grilla_global_del_negocio(self):
        # Semántica definida: la consulta SIN resource_id representa la
        # disponibilidad del negocio completo, por lo que un turno confirmado
        # con recurso también la bloquea.
        self._create(resource_id=self.court1, time="09:00")
        slots = get_available_times(self.date, 1, service="Corte")
        self.assertNotIn("09:00", slots)

    def test_disponibilidad_por_recurso_excluye_bloqueo_global(self):
        self._create(resource_id=None, time="09:00")
        slots = get_available_times(self.date, 1, service="Corte",
                                    resource_id=self.court2)
        self.assertNotIn("09:00", slots)

    def test_disponibilidad_por_recurso_excluye_solo_su_recurso(self):
        self._create(resource_id=self.court1, time="09:00")
        slots_c2 = get_available_times(self.date, 1, service="Corte",
                                       resource_id=self.court2)
        slots_c1 = get_available_times(self.date, 1, service="Corte",
                                       resource_id=self.court1)
        self.assertIn("09:00", slots_c2)
        self.assertNotIn("09:00", slots_c1)

    def test_resource_id_de_otro_negocio_rechazado(self):
        from services.appointments import _validate_resource
        validated, reason = _validate_resource(self.court1, 2)
        self.assertIsNone(validated)
        self.assertEqual(reason, "invalid_resource")


class TestResourceLifecycle(BaseResourceBookingTest):

    def setUp(self):
        super().setUp()
        self.court1 = database.create_resource_scoped(1, "Cancha 1")

    def test_cancelacion_no_altera_resource_id(self):
        result = self._create(resource_id=self.court1, time="09:00")
        self.assertTrue(result["success"])
        # La cancelación exige segundo factor: nombre + teléfono del titular.
        cancelado = cancel_appointment(
            result["appointment_id"],
            "111111111",
            business_id=1,
            customer_name="Cliente Test",
        )
        self.assertTrue(cancelado)
        fila = self._query(
            "SELECT resource_id, status FROM appointments WHERE id = ?",
            (result["appointment_id"],),
        )[0]
        self.assertEqual(fila["resource_id"], self.court1)
        self.assertEqual(fila["status"], "cancelled")

    def test_reprogramacion_conserva_resource_id(self):
        result = self._create(resource_id=self.court1, time="09:00")
        self.assertTrue(result["success"])
        nuevo_dia = _next_open_day()
        reschedule = reschedule_appointment_admin(
            result["appointment_id"], nuevo_dia, "11:00", business_id=1,
        )
        self.assertTrue(reschedule["success"])
        self.assertEqual(reschedule["resource_id"], self.court1)
        fila = self._query(
            "SELECT resource_id, appointment_time FROM appointments WHERE id = ?",
            (result["appointment_id"],),
        )[0]
        self.assertEqual(fila["resource_id"], self.court1)
        self.assertEqual(fila["appointment_time"], "11:00")

    def test_reprogramacion_de_recurso_respeta_solapamiento(self):
        r1 = self._create(resource_id=self.court1, time="09:00")
        r2 = self._create(resource_id=self.court1, time="11:00",
                          name="Otro Cliente")
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])
        # Mover r1 a las 11:00 debe chocar con r2 (mismo recurso).
        moved = reschedule_appointment_admin(
            r1["appointment_id"], self.date, "11:00", business_id=1,
        )
        self.assertFalse(moved["success"])
        self.assertEqual(moved["reason"], "occupied")


if __name__ == "__main__":
    unittest.main()
