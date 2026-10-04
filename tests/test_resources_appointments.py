"""Etapa 1: recursos reservables — comportamiento de reservas.

Ejecuta contra PostgreSQL mediante `tests._pg_compat.PostgreSQLTestCase`: cada
test recibe una base `turnobot_test_<uuid>` desechable con el esquema aplicado y
la semilla estándar (`seed_standard_test_data`), que aporta el negocio 1
"El Corte" con sus horarios semanales y el servicio "Corte" de 30 minutos.
No hay swap de `DATABASE_PATH` ni SQLite: el aislamiento por tenant es el de la
base temporal, no el de un archivo temporal.

Cubre:
- Negocio sin recursos mantiene el comportamiento anterior.
- Dos recursos distintos pueden reservarse simultáneamente.
- El mismo recurso no admite dos reservas solapadas.
- Una reserva global (resource_id NULL) bloquea los recursos.
- Una reserva de recurso no bloquea otro recurso.
- Disponibilidad por recurso (get_available_times).
- Cancelación y reprogramación conservan el resource_id.
"""

import unittest
from datetime import datetime, timedelta

import database.database as database
from database.database import get_connection
from services.appointments import (
    cancel_appointment,
    create_appointment,
    get_available_times,
    reschedule_appointment_admin,
)
from tests._pg_compat import PostgreSQLTestCase


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


def _fmt_time(value):
    """Normaliza la columna TIME a 'HH:MM' para la aserción.

    SQLite la devuelve como TEXT y PostgreSQL como `datetime.time`; la
    comparación es la misma en ambos backends una vez normalizada.
    """
    if isinstance(value, str):
        return value
    return value.strftime("%H:%M")


class BaseResourceBookingTest(unittest.TestCase, PostgreSQLTestCase):
    """Negocio 1 (semilla estándar) sobre una base PostgreSQL aislada por test."""

    def setUp(self):
        self.date = _next_open_day()
        # Un único contexto de app por test: los servicios de appointments
        # resuelven el pool mediante `flask.current_app`, así que crear,
        # cancelar, reprogramar y consultar disponibilidad deben correr dentro
        # del contexto de la app que apunta a la base temporal de este test.
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.addCleanup(self._app_context.pop)

    def _create(
        self, business_id=1, time="09:00", resource_id=None, service="Corte", name="Cliente Test"
    ):
        return create_appointment(
            name, "111111111", service, self.date, time, business_id, resource_id=resource_id
        )

    def _create_resource(self, name, business_id=1):
        return database.create_resource_scoped(business_id, name)

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
        self.court1 = self._create_resource("Cancha 1")
        self.court2 = self._create_resource("Cancha 2")

    def test_dos_recursos_distintos_mismo_horario(self):
        r1 = self._create(resource_id=self.court1)
        r2 = self._create(resource_id=self.court2, name="Otro Cliente")
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])

    def test_mismo_recurso_no_admite_solapamiento(self):
        # "Corte" dura 30 min: dos reservas a la misma hora solapan en el
        # mismo recurso.
        r1 = self._create(resource_id=self.court1, time="09:00")
        r2 = self._create(resource_id=self.court1, time="09:00", name="Otro Cliente")
        self.assertTrue(r1["success"])
        self.assertFalse(r2["success"])
        self.assertEqual(r2["reason"], "occupied")

    def test_reserva_global_bloquea_los_recursos(self):
        r_global = self._create(resource_id=None, time="09:00")
        r_recurso = self._create(resource_id=self.court1, time="09:00", name="Otro Cliente")
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
        slots = get_available_times(self.date, 1, service="Corte", resource_id=self.court2)
        self.assertNotIn("09:00", slots)

    def test_disponibilidad_por_recurso_excluye_solo_su_recurso(self):
        self._create(resource_id=self.court1, time="09:00")
        slots_c2 = get_available_times(self.date, 1, service="Corte", resource_id=self.court2)
        slots_c1 = get_available_times(self.date, 1, service="Corte", resource_id=self.court1)
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
        self.court1 = self._create_resource("Cancha 1")

    def test_cancelacion_no_altera_resource_id(self):
        result = self._create(resource_id=self.court1, time="09:00")
        self.assertTrue(result["success"])
        # La cancelación exige el management_token del turno.
        cancelado = cancel_appointment(
            result["appointment_id"],
            "111111111",
            business_id=1,
            management_token=result["management_token"],
            customer_name="Cliente Test",
        )
        self.assertTrue(cancelado)
        fila = self._query(
            "SELECT resource_id, status FROM appointments WHERE id = %s",
            (result["appointment_id"],),
        )[0]
        self.assertEqual(fila["resource_id"], self.court1)
        self.assertEqual(fila["status"], "cancelled")

    def test_reprogramacion_conserva_resource_id(self):
        result = self._create(resource_id=self.court1, time="09:00")
        self.assertTrue(result["success"])
        nuevo_dia = _next_open_day()
        reschedule = reschedule_appointment_admin(
            result["appointment_id"], nuevo_dia, "11:00", business_id=1
        )
        self.assertTrue(reschedule["success"])
        self.assertEqual(reschedule["resource_id"], self.court1)
        fila = self._query(
            "SELECT resource_id, appointment_time FROM appointments WHERE id = %s",
            (result["appointment_id"],),
        )[0]
        self.assertEqual(fila["resource_id"], self.court1)
        self.assertEqual(_fmt_time(fila["appointment_time"]), "11:00")

    def test_reprogramacion_de_recurso_respeta_solapamiento(self):
        r1 = self._create(resource_id=self.court1, time="09:00")
        r2 = self._create(resource_id=self.court1, time="11:00", name="Otro Cliente")
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])
        # Mover r1 a las 11:00 debe chocar con r2 (mismo recurso).
        moved = reschedule_appointment_admin(
            r1["appointment_id"], self.date, "11:00", business_id=1
        )
        self.assertFalse(moved["success"])
        self.assertEqual(moved["reason"], "occupied")


if __name__ == "__main__":
    unittest.main()
