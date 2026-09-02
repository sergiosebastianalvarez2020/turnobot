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
            "UPDATE businesses SET name = 'Business A', slug = 'business-a' WHERE id = 1"
        )
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

    def _insert_confirmed_appointment(self, name, phone, business_id):
        self._execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time, status, business_id) VALUES (?, ?, ?, ?, ?, 'confirmed', ?)",
            (name, phone, "Servicio", self.valid_date, "09:00", business_id),
        )

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

    def test_api_turnos_business_a_ve_solo_sus_turnos(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response = self.client.get(
            f"/b/business-a/api/turnos?nombre=Cliente+A&telefono=111111111"
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["turnos"]), 1)
        self.assertEqual(data["turnos"][0]["customer_name"], "Cliente A")

    def test_api_turnos_business_b_ve_solo_sus_turnos(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response = self.client.get(
            f"/b/business-b/api/turnos?nombre=Cliente+B&telefono=222222222"
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["turnos"]), 1)
        self.assertEqual(data["turnos"][0]["customer_name"], "Cliente B")

    def test_api_turnos_no_cruza_de_a_a_b(self):
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response = self.client.get(
            f"/b/business-a/api/turnos?nombre=Cliente+B&telefono=222222222"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turnos"], [])

    def test_api_turnos_no_cruza_de_b_a_a(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)

        response = self.client.get(
            f"/b/business-b/api/turnos?nombre=Cliente+A&telefono=111111111"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turnos"], [])

    def test_api_turnos_cambiar_telefono_no_cruza_de_business(self):
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response = self.client.get(
            f"/b/business-a/api/turnos?nombre=Cliente+B&telefono=222222222"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turnos"], [])

    def test_api_turnos_cambiar_nombre_no_cruza_de_business(self):
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response = self.client.get(
            f"/b/business-a/api/turnos?nombre=Cliente+B&telefono=222222222"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turnos"], [])

    def test_api_turnos_business_id_artificial_no_cambia_el_tenant(self):
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response = self.client.get(
            f"/b/business-a/api/turnos?nombre=Cliente+B&telefono=222222222&business_id=2"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["turnos"], [])

    def test_api_turnos_slug_inexistente_devuelve_404_sin_fallback(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)

        response = self.client.get(
            f"/b/slug-inexistente/api/turnos?nombre=Cliente+A&telefono=111111111"
        )

        self.assertEqual(response.status_code, 404)

    def test_api_turnos_legacy_continua_funcionando(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)

        response = self.client.get(
            f"/api/turnos?nombre=Cliente+A&telefono=111111111"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(len(response.get_json()["turnos"]), 1)

    def test_api_turnos_el_corte_continua_funcionando(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)
        self._execute(
            "UPDATE businesses SET name = 'El Corte', slug = 'el-corte' WHERE id = 1"
        )

        response = self.client.get(
            f"/b/el-corte/api/turnos?nombre=Cliente+A&telefono=111111111"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_api_turnos_dos_requests_consecutivos_mantienen_tenant(self):
        self._insert_confirmed_appointment("Cliente A", "111111111", 1)
        self._insert_confirmed_appointment("Cliente B", "222222222", 2)

        response_a = self.client.get(
            f"/b/business-a/api/turnos?nombre=Cliente+A&telefono=111111111"
        )
        response_b = self.client.get(
            f"/b/business-b/api/turnos?nombre=Cliente+B&telefono=222222222"
        )

        self.assertEqual(response_a.get_json()["turnos"][0]["customer_name"], "Cliente A")
        self.assertEqual(response_b.get_json()["turnos"][0]["customer_name"], "Cliente B")

    def test_api_turnos_mantiene_estructura_json_y_validaciones(self):
        response = self.client.get("/b/business-a/api/turnos")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "El nombre es obligatorio.")

        response = self.client.get("/b/business-a/api/turnos?nombre=Cliente+A")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "El teléfono es obligatorio para consultar tus turnos.",
        )

    def _reservation_payload(self, service, name="Cliente", phone="123456789", **extra):
        payload = {
            "nombre": name,
            "telefono": phone,
            "servicio": service,
            "fecha": self.valid_date,
            "hora": "09:00",
        }
        payload.update(extra)
        return payload

    def test_api_reservar_scoped_business_a_crea_turno_propio(self):
        response = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", name="Cliente A"),
        )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertIn("appointment_id", data)
        self.assertEqual(
            self._query("SELECT business_id FROM appointments WHERE id = ?", (data["appointment_id"],))[0][0],
            1,
        )

    def test_api_reservar_scoped_business_b_crea_turno_propio(self):
        response = self.client.post(
            "/b/business-b/api/reservar",
            json=self._reservation_payload("Servicio B", name="Cliente B", phone="987654321"),
        )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(
            self._query("SELECT business_id FROM appointments WHERE id = ?", (data["appointment_id"],))[0][0],
            2,
        )

    def test_api_reservar_scoped_rechaza_servicio_de_otro_business(self):
        response = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Servicio B"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "El servicio seleccionado no es válido.")
        self.assertEqual(self._query("SELECT COUNT(*) FROM appointments")[0][0], 0)

    def test_api_reservar_scoped_business_b_rechaza_servicio_de_a(self):
        response = self.client.post(
            "/b/business-b/api/reservar",
            json=self._reservation_payload("Corte"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "El servicio seleccionado no es válido.")
        self.assertEqual(self._query("SELECT COUNT(*) FROM appointments")[0][0], 0)

    def test_api_reservar_business_id_json_no_cambia_el_tenant(self):
        response = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", business_id=2),
        )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(
            self._query("SELECT business_id FROM appointments WHERE id = ?", (data["appointment_id"],))[0][0],
            1,
        )

    def test_api_reservar_precio_del_cliente_no_controla_backend(self):
        response = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", precio=0),
        )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        appointment = self._query(
            "SELECT business_id, service FROM appointments WHERE id = ?",
            (data["appointment_id"],),
        )[0]
        self.assertEqual(appointment["business_id"], 1)
        self.assertEqual(appointment["service"], "Corte")

    def test_api_reservar_duracion_del_cliente_no_controla_backend(self):
        response = self.client.post(
            "/b/business-b/api/reservar",
            json=self._reservation_payload("Servicio B", duracion=1),
        )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        appointment = self._query(
            "SELECT business_id, service FROM appointments WHERE id = ?",
            (data["appointment_id"],),
        )[0]
        self.assertEqual(appointment["business_id"], 2)
        self.assertEqual(appointment["service"], "Servicio B")

    def test_api_reservar_misma_hora_permitida_entre_businesses(self):
        response_a = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", name="Cliente A"),
        )
        response_b = self.client.post(
            "/b/business-b/api/reservar",
            json=self._reservation_payload("Servicio B", name="Cliente B"),
        )

        self.assertEqual(response_a.status_code, 201)
        self.assertEqual(response_b.status_code, 201)

    def test_api_reservar_doble_reserva_mismo_business_continua_bloqueada(self):
        payload_a = self._reservation_payload("Corte", name="Cliente A")
        response_first = self.client.post("/b/business-a/api/reservar", json=payload_a)
        response_second = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", name="Cliente A 2", phone="987654321"),
        )

        self.assertEqual(response_first.status_code, 201)
        self.assertEqual(response_second.status_code, 400)
        self.assertEqual(response_second.get_json()["reason"], "occupied")

    def test_api_reservar_slug_inexistente_devuelve_404_y_no_crea_turno(self):
        response = self.client.post(
            "/b/slug-inexistente/api/reservar",
            json=self._reservation_payload("Corte"),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._query("SELECT COUNT(*) FROM appointments")[0][0], 0)

    def test_api_reservar_el_corte_scoped_continua_funcionando(self):
        self._execute(
            "UPDATE businesses SET name = 'El Corte', slug = 'el-corte' WHERE id = 1"
        )
        response = self.client.post(
            "/b/el-corte/api/reservar",
            json=self._reservation_payload("Corte"),
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["success"])

    def test_api_reservar_dos_requests_consecutivos_no_contaminan_tenant(self):
        response_a = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", name="Cliente A"),
        )
        response_b = self.client.post(
            "/b/business-b/api/reservar",
            json=self._reservation_payload("Servicio B", name="Cliente B"),
        )

        self.assertEqual(response_a.status_code, 201)
        self.assertEqual(response_b.status_code, 201)
        rows = self._query(
            "SELECT customer_name, business_id FROM appointments ORDER BY id"
        )
        self.assertEqual([(row["customer_name"], row["business_id"]) for row in rows], [
            ("Cliente A", 1),
            ("Cliente B", 2),
        ])

    def test_api_reservar_estructura_y_razones_de_error_compatibles(self):
        response = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", fecha="fecha-invalida"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "invalid_date")

        response = self.client.post(
            "/b/business-a/api/reservar",
            json=self._reservation_payload("Corte", hora="hora-invalida"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "invalid_time")

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


class TestPublicApiAvailabilityIsolation(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        weekday = next_open_day()
        day_index = datetime.fromisoformat(weekday).weekday()

        self._execute(
            "UPDATE businesses SET name = 'Business A', slug = 'business-a' WHERE id = 1"
        )
        self._execute(
            "UPDATE business_settings SET business_name = 'Business A', business_type = 'Barbería', business_initials = 'BA', business_description = 'Negocio A', timezone = 'America/Argentina/Buenos_Aires', slot_duration = 30, break_between_slots = 0, business_id = 1 WHERE id = 1"
        )
        self._execute(
            "UPDATE services SET name = 'Corte A', price = 20000, duration = 30, active = 1, business_id = 1 WHERE id = 1"
        )
        self._execute(
            "UPDATE services SET name = 'Barba A', price = 15000, duration = 20, active = 1, business_id = 1 WHERE id = 2"
        )
        self._execute(
            "UPDATE weekly_schedules SET is_open = 1, morning_start = '09:00', morning_end = '10:30', afternoon_start = NULL, afternoon_end = NULL WHERE business_id = 1 AND day_of_week = ?",
            (day_index,),
        )

        self._execute(
            "INSERT OR REPLACE INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )
        self._execute(
            "INSERT OR REPLACE INTO services (id, name, price, duration, active, business_id) VALUES (4, 'Corte B', 22000, 60, 1, 2)"
        )
        self._execute(
            "INSERT OR REPLACE INTO weekly_schedules (id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end, business_id) VALUES (10, ?, 1, '11:00', '12:00', NULL, NULL, 2)",
            (day_index,),
        )

        self.valid_date = weekday
        self.client = application.app.test_client()

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

    def test_api_disponibilidad_uses_business_a_when_route_is_business_a(self):
        api_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        self.assertEqual(api_response.status_code, 200)
        data = api_response.get_json()
        self.assertIn("09:00", data["horarios_disponibles"])
        self.assertIn("09:30", data["horarios_disponibles"])
        self.assertIn("10:00", data["horarios_disponibles"])
        self.assertNotIn("11:00", data["horarios_disponibles"])

    def test_api_disponibilidad_uses_business_b_when_route_is_business_b(self):
        api_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        self.assertEqual(api_response.status_code, 200)
        data = api_response.get_json()
        self.assertIn("11:00", data["horarios_disponibles"])
        self.assertNotIn("09:00", data["horarios_disponibles"])

    def test_business_a_slots_do_not_appear_for_business_b(self):
        a_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        slots_a = set(a_response.get_json()["horarios_disponibles"])

        b_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        slots_b = set(b_response.get_json()["horarios_disponibles"])

        self.assertTrue(slots_a.isdisjoint(slots_b))
        self.assertNotIn("11:00", slots_a)
        self.assertNotIn("09:00", slots_b)

    def test_business_b_slots_do_not_appear_for_business_a(self):
        b_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        slots_b = set(b_response.get_json()["horarios_disponibles"])

        a_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        slots_a = set(a_response.get_json()["horarios_disponibles"])

        self.assertTrue(slots_a.isdisjoint(slots_b))
        self.assertNotIn("11:00", slots_a)
        self.assertNotIn("09:00", slots_b)

    def test_confirmed_appointment_in_a_blocks_only_a(self):
        self._execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time, status, business_id) VALUES (?, ?, ?, ?, ?, 'confirmed', 1)",
            ("Cliente A", "123456789", "Corte A", self.valid_date, "09:00",),
        )

        a_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        self.assertNotIn("09:00", a_response.get_json()["horarios_disponibles"])

        b_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        self.assertIn("11:00", b_response.get_json()["horarios_disponibles"])

    def test_confirmed_appointment_in_b_blocks_only_b(self):
        self._execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time, status, business_id) VALUES (?, ?, ?, ?, ?, 'confirmed', 2)",
            ("Cliente B", "987654321", "Corte B", self.valid_date, "11:00",),
        )

        b_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        self.assertNotIn("11:00", b_response.get_json()["horarios_disponibles"])

        a_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        self.assertIn("09:00", a_response.get_json()["horarios_disponibles"])

    def test_closed_day_for_a_does_not_return_business_b_slots(self):
        self._execute(
            "UPDATE weekly_schedules SET is_open = 0, morning_start = NULL, morning_end = NULL, afternoon_start = NULL, afternoon_end = NULL WHERE business_id = 1 AND day_of_week = ?",
            (datetime.fromisoformat(self.valid_date).weekday(),),
        )

        a_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        self.assertEqual(a_response.get_json()["horarios_disponibles"], [])

        b_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        self.assertIn("11:00", b_response.get_json()["horarios_disponibles"])

    def test_slot_duration_does_not_mix_between_businesses(self):
        a_response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}")
        a_slots = set(a_response.get_json()["horarios_disponibles"])

        b_response = self.client.get(f"/b/business-b/api/disponibilidad/{self.valid_date}")
        b_slots = set(b_response.get_json()["horarios_disponibles"])

        self.assertIn("09:00", a_slots)
        self.assertIn("09:30", a_slots)
        self.assertIn("11:00", b_slots)
        self.assertNotIn("09:00", b_slots)

    def test_business_id_from_client_is_ignored(self):
        response = self.client.get(f"/b/business-a/api/disponibilidad/{self.valid_date}?business_id=2")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("09:00", data["horarios_disponibles"])
        self.assertNotIn("11:00", data["horarios_disponibles"])

    def test_legacy_api_disponibilidad_still_works(self):
        response = self.client.get(f"/api/disponibilidad/{self.valid_date}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("horarios_disponibles", response.get_json())

    def test_business_el_corte_api_disponibilidad_still_works(self):
        self._execute(
            "UPDATE businesses SET name = 'El Corte', slug = 'el-corte' WHERE id = 1"
        )
        self._execute(
            "UPDATE business_settings SET business_name = 'El Corte', business_type = 'Barbería', business_initials = 'EC', business_description = 'Negocio principal', timezone = 'America/Argentina/Buenos_Aires', slot_duration = 30, break_between_slots = 0, business_id = 1 WHERE id = 1"
        )
        response = self.client.get(f"/b/el-corte/api/disponibilidad/{self.valid_date}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("horarios_disponibles", response.get_json())

    def test_slug_inexistente_deve_devolver_404(self):
        response = self.client.get(f"/b/slug-inexistente/api/disponibilidad/{self.valid_date}")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
