import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as application
import database.database as database


def next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class TestPublicApiServiceIsolation(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self._execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )
        self._execute(
            "INSERT INTO services (business_id, name, price, duration, active) "
            "VALUES (2, 'Servicio B', 15000, 30, 1)"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 0, 1, '09:00', '13:00', '15:00', '19:00')"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 1, 1, '09:00', '13:00', '15:00', '19:00')"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 2, 1, '09:00', '13:00', '15:00', '19:00')"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 3, 1, '09:00', '13:00', '15:00', '19:00')"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 4, 1, '09:00', '13:00', '15:00', '19:00')"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 5, 1, '09:00', '13:00', '15:00', '19:00')"
        )
        self._execute(
            "INSERT INTO weekly_schedules "
            "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
            "VALUES (2, 6, 1, '09:00', '13:00', '15:00', '19:00')"
        )

        self.client = application.app.test_client()
        self.valid_date = next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _execute(sql, params=None):
        connection = database.get_connection()
        try:
            connection.execute(sql, params or ())
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _query(sql, params=None):
        connection = database.get_connection()
        try:
            return connection.execute(sql, params or ()).fetchall()
        finally:
            connection.close()

    def _business(self, business_id):
        return {
            1: {"id": 1, "name": "El Corte", "slug": "el-corte"},
            2: {"id": 2, "name": "Business B", "slug": "business-b"},
        }[business_id]

    def test_api_servicios_business_a_ve_solo_sus_servicios(self):
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.get("/api/servicios")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        nombres = {item["nombre"] for item in data["servicios"]}
        self.assertEqual(nombres, {"Corte", "Corte + barba", "Barba"})

    def test_api_servicios_business_b_ve_solo_sus_servicios(self):
        with patch.object(application, "resolve_business", return_value=self._business(2)):
            response = self.client.get("/api/servicios")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        nombres = {item["nombre"] for item in data["servicios"]}
        self.assertEqual(nombres, {"Servicio B"})

    def test_api_servicios_business_a_no_recibe_servicios_de_b(self):
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.get("/api/servicios")
        data = response.get_json()
        self.assertNotIn("Servicio B", {item["nombre"] for item in data["servicios"]})

    def test_api_servicios_business_b_no_recibe_servicios_de_a(self):
        with patch.object(application, "resolve_business", return_value=self._business(2)):
            response = self.client.get("/api/servicios")
        data = response.get_json()
        self.assertNotIn("Corte", {item["nombre"] for item in data["servicios"]})
        self.assertNotIn("Corte + barba", {item["nombre"] for item in data["servicios"]})
        self.assertNotIn("Barba", {item["nombre"] for item in data["servicios"]})

    def test_api_reservar_business_a_puede_reservar_servicio_de_a(self):
        payload = {
            "nombre": "Cliente A",
            "telefono": "123456789",
            "servicio": "Corte",
            "fecha": self.valid_date,
            "hora": "09:00",
        }
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.post("/api/reservar", json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 1")[0][0],
            1,
        )

    def test_api_reservar_business_b_puede_reservar_servicio_de_b(self):
        payload = {
            "nombre": "Cliente B",
            "telefono": "987654321",
            "servicio": "Servicio B",
            "fecha": self.valid_date,
            "hora": "09:00",
        }
        with patch.object(application, "resolve_business", return_value=self._business(2)):
            response = self.client.post("/api/reservar", json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 2")[0][0],
            1,
        )

    def test_api_reservar_business_a_no_puede_usar_servicio_de_b(self):
        payload = {
            "nombre": "Cliente A",
            "telefono": "123456789",
            "servicio": "Servicio B",
            "fecha": self.valid_date,
            "hora": "09:00",
        }
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.post("/api/reservar", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 1")[0][0],
            0,
        )

    def test_api_reservar_business_b_no_puede_usar_servicio_de_a(self):
        payload = {
            "nombre": "Cliente B",
            "telefono": "987654321",
            "servicio": "Corte",
            "fecha": self.valid_date,
            "hora": "09:00",
        }
        with patch.object(application, "resolve_business", return_value=self._business(2)):
            response = self.client.post("/api/reservar", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 2")[0][0],
            0,
        )

    def test_business_id_inyectado_por_cliente_no_cambia_el_tenant(self):
        payload = {
            "nombre": "Cliente A",
            "telefono": "123456789",
            "servicio": "Corte",
            "fecha": self.valid_date,
            "hora": "09:00",
            "business_id": 2,
        }
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.post("/api/reservar", json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 1")[0][0],
            1,
        )
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 2")[0][0],
            0,
        )

    def test_reserva_valida_sigue_creando_appointment_en_el_business_correcto(self):
        payload = {
            "nombre": "Cliente A",
            "telefono": "123456789",
            "servicio": "Corte",
            "fecha": self.valid_date,
            "hora": "09:00",
        }
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.post("/api/reservar", json=payload)
        data = response.get_json()
        self.assertTrue(data["success"])
        appointment = self._query(
            "SELECT business_id, customer_name, service FROM appointments WHERE customer_name = ?",
            ("Cliente A",),
        )[0]
        self.assertEqual(appointment["business_id"], 1)
        self.assertEqual(appointment["service"], "Corte")

    def test_root_continua_funcionando(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_b_el_corte_continua_funcionando(self):
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.get("/b/el-corte")
        self.assertEqual(response.status_code, 200)

    def test_servicios_de_el_corte_sigue_igual(self):
        with patch.object(application, "resolve_business", return_value=self._business(1)):
            response = self.client.get("/api/servicios")
        data = response.get_json()
        nombres = [item["nombre"] for item in data["servicios"]]
        self.assertEqual(nombres, ["Corte", "Corte + barba", "Barba"])


if __name__ == "__main__":
    unittest.main()
