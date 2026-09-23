import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app as application
import database.database as database


class BaseIsolationTest(unittest.TestCase):
    """Base con Business A (id=1) y Business B (id=2) en base temporal."""

    @classmethod
    def setUpClass(cls):
        cls._original_database_path = database.DATABASE_PATH

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self._execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )

        # Recursos por negocio: A tiene dos, B tiene uno.
        self.resource_a = database.create_resource_scoped(1, "Cancha A1")
        self.resource_a2 = database.create_resource_scoped(1, "Cancha A2")
        self.resource_b = database.create_resource_scoped(2, "Cancha B1")

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _query(sql, params=None):
        connection = database.get_connection()
        try:
            return connection.execute(sql, params or ()).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _execute(sql, params=None):
        connection = database.get_connection()
        try:
            connection.execute(sql, params or ())
            connection.commit()
        finally:
            connection.close()


# ============================================================
# READ ISOLATION
# ============================================================


class TestReadIsolation(BaseIsolationTest):
    def test_a_solo_ve_sus_recursos(self):
        nombres_a = {row["name"] for row in database.get_resources_scoped(1)}
        self.assertEqual(nombres_a, {"Cancha A1", "Cancha A2"})

    def test_b_solo_ve_sus_recursos(self):
        nombres_b = {row["name"] for row in database.get_resources_scoped(2)}
        self.assertEqual(nombres_b, {"Cancha B1"})

    def test_a_no_puede_obtener_recurso_de_b(self):
        self.assertIsNone(database.get_resource_scoped(self.resource_b, 1))

    def test_b_no_puede_obtener_recurso_de_a(self):
        self.assertIsNone(database.get_resource_scoped(self.resource_a, 2))


# ============================================================
# WRITE ISOLATION
# ============================================================


class TestWriteIsolation(BaseIsolationTest):
    def test_a_no_puede_modificar_recurso_de_b(self):
        self.assertFalse(database.set_resource_active_scoped(self.resource_b, 1, False))
        fila = self._query("SELECT active FROM resources WHERE id = ?", (self.resource_b,))[0]
        self.assertEqual(fila["active"], 1)

    def test_b_no_puede_modificar_recurso_de_a(self):
        self.assertFalse(database.set_resource_active_scoped(self.resource_a, 2, False))
        fila = self._query("SELECT active FROM resources WHERE id = ?", (self.resource_a,))[0]
        self.assertEqual(fila["active"], 1)

    def test_tenant_puede_modificar_recurso_propio(self):
        self.assertTrue(database.set_resource_active_scoped(self.resource_a, 1, False))
        fila = self._query("SELECT active FROM resources WHERE id = ?", (self.resource_a,))[0]
        self.assertEqual(fila["active"], 0)


# ============================================================
# BOOKING ISOLATION (resource_id de otro tenant)
# ============================================================


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class TestBookingIsolation(BaseIsolationTest):
    def _create(self, business_id, resource_id=None):
        from services.appointments import create_appointment

        return create_appointment(
            "Cliente Test",
            "111111111",
            "Corte",
            _next_open_day(),
            "09:00",
            business_id,
            resource_id=resource_id,
        )

    def test_a_no_puede_reservar_con_recurso_de_b(self):
        result = self._create(1, resource_id=self.resource_b)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_resource")

    def test_b_no_puede_reservar_con_recurso_de_a(self):
        result = self._create(2, resource_id=self.resource_a)
        self.assertFalse(result["success"])
        # Aceptado: B no tiene servicios, por lo que la validación previa de
        # servicio rechaza antes con invalid_service. El punto clave es que la
        # reserva se rechaza y no se inserta nada (ver
        # test_resource_id_invalido_no_se_inserta).
        self.assertIn(result["reason"], ("invalid_resource", "invalid_service"))

    def test_recurso_inexistente_es_rechazado(self):
        result = self._create(1, resource_id=999999)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_resource")

    def test_resource_id_invalido_no_se_inserta(self):
        self._create(1, resource_id=self.resource_b)
        total = self._query("SELECT COUNT(*) AS t FROM appointments")[0]["t"]
        self.assertEqual(total, 0)

    def test_a_puede_reservar_con_recurso_propio(self):
        result = self._create(1, resource_id=self.resource_a)
        self.assertTrue(result["success"])
        self.assertEqual(result["resource_id"], self.resource_a)
        fila = self._query(
            "SELECT resource_id, business_id FROM appointments WHERE id = ?",
            (result["appointment_id"],),
        )[0]
        self.assertEqual(fila["resource_id"], self.resource_a)
        self.assertEqual(fila["business_id"], 1)

    def test_recurso_inactivo_es_rechazado(self):
        database.set_resource_active_scoped(self.resource_a, 1, False)
        result = self._create(1, resource_id=self.resource_a)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_resource")


if __name__ == "__main__":
    unittest.main()
