import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from services import appointments


def next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class BaseAppointmentIsolationTest(unittest.TestCase):
    """Business A (id=1) y Business B (id=2) en base temporal."""

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        # Business B
        self._execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )
        # Business B necesita servicios propios para reservar
        self._execute(
            "INSERT INTO services (business_id, name, price, duration, active) "
            "VALUES (2, 'Corte B', 12000, 30, 1)"
        )
        # Business B necesita horarios semanales para generar slots
        for day in range(7):
            self._execute(
                "INSERT INTO weekly_schedules "
                "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
                "VALUES (2, ?, 1, '09:00', '13:00', '15:00', '19:00')",
                (day,),
            )
        self.valid_date = next_open_day()

        self.client = application.app.test_client()
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def login(self, business_id):
        business = {
            1: {"id": 1, "name": "El Corte", "slug": "el-corte"},
            2: {"id": 2, "name": "Business B", "slug": "business-b"},
        }[business_id]
        login_page = self.client.get("/login")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', login_page.text
        ).group(1)
        self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": token},
        )
        with patch.object(application, "resolve_business", return_value=business):
            with application.app.test_request_context("/"):
                application.load_current_business()
                self.csrf_token = token

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

    @staticmethod
    def _business_id_of(appointment_id):
        return BaseAppointmentIsolationTest._query(
            "SELECT business_id FROM appointments WHERE id = ?",
            (appointment_id,),
        )[0]["business_id"]


# ============================================================
# READ ISOLATION (LISTADO / BÚSQUEDA)
# ============================================================

class TestAppointmentReadIsolation(BaseAppointmentIsolationTest):

    def test_business_a_solo_ve_sus_turnos(self):
        appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )
        appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "10:00", business_id=2
        )
        turnos_a = appointments.get_appointments(business_id=1)
        nombres_a = {t["customer_name"] for t in turnos_a}
        self.assertIn("Cliente A", nombres_a)
        self.assertNotIn("Cliente B", nombres_a)

    def test_business_b_solo_ve_sus_turnos(self):
        appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )
        appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "10:00", business_id=2
        )
        turnos_b = appointments.get_appointments(business_id=2)
        nombres_b = {t["customer_name"] for t in turnos_b}
        self.assertIn("Cliente B", nombres_b)
        self.assertNotIn("Cliente A", nombres_b)

    def test_busqueda_por_cliente_filtra_por_negocio(self):
        appointments.create_appointment(
            "Cliente X", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )
        appointments.create_appointment(
            "Cliente X", "3838439333", "Corte B", self.valid_date, "10:00", business_id=2
        )
        turnos_b = appointments.get_customer_appointments(
            "Cliente X", "3838439333", business_id=2
        )
        self.assertEqual(len(turnos_b), 1)


# ============================================================
# CREATE ASSOCIATION
# ============================================================

class TestAppointmentCreateAssociation(BaseAppointmentIsolationTest):

    def test_turno_creado_por_a_queda_asociado_a_a(self):
        result = appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            self._business_id_of(result["appointment_id"]), 1
        )

    def test_turno_creado_por_b_queda_asociado_a_b(self):
        result = appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "09:00", business_id=2
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            self._business_id_of(result["appointment_id"]), 2
        )

    def test_create_appointment_valida_servicio_del_mismo_negocio(self):
        # B intenta reservar "Corte" que pertenece a A -> invalid_service
        result = appointments.create_appointment(
            "Cliente B", "3838439333", "Corte", self.valid_date, "09:00", business_id=2
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_service")


# ============================================================
# CANCEL ISOLATION
# ============================================================

class TestCancelIsolation(BaseAppointmentIsolationTest):

    def test_a_no_puede_cancelar_turno_de_b(self):
        turno_b = appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "09:00", business_id=2
        )["appointment_id"]
        cancelado = appointments.cancel_appointment(
            turno_b, "3838439333", business_id=1
        )
        self.assertFalse(cancelado)
        estado = self._query(
            "SELECT status FROM appointments WHERE id = ?", (turno_b,)
        )[0]["status"]
        self.assertEqual(estado, "confirmed")

    def test_b_no_puede_cancelar_turno_de_a(self):
        turno_a = appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )["appointment_id"]
        cancelado = appointments.cancel_appointment(
            turno_a, "3838439222", business_id=2
        )
        self.assertFalse(cancelado)
        estado = self._query(
            "SELECT status FROM appointments WHERE id = ?", (turno_a,)
        )[0]["status"]
        self.assertEqual(estado, "confirmed")


# ============================================================
# RESCHEDULE ISOLATION
# ============================================================

class TestRescheduleIsolation(BaseAppointmentIsolationTest):

    def test_a_no_puede_reprogramar_turno_de_b(self):
        turno_b = appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "09:00", business_id=2
        )["appointment_id"]
        result = appointments.reschedule_appointment(
            turno_b, self.valid_date, "10:00", "3838439333", business_id=1
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")

    def test_b_no_puede_reprogramar_turno_de_a(self):
        turno_a = appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )["appointment_id"]
        result = appointments.reschedule_appointment(
            turno_a, self.valid_date, "10:00", "3838439222", business_id=2
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")


# ============================================================
# AVAILABILITY ISOLATION (Ocupación por negocio)
# ============================================================

class TestAvailabilityIsolation(BaseAppointmentIsolationTest):

    def test_ocupacion_de_a_no_afecta_disponibilidad_de_b(self):
        appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )
        horarios_a = appointments.get_available_times(self.valid_date, business_id=1)
        horarios_b = appointments.get_available_times(self.valid_date, business_id=2)
        # A tiene ocupado el 09:00, B NO
        self.assertNotIn("09:00", horarios_a)
        self.assertIn("09:00", horarios_b)

    def test_mismo_horario_distinto_negocio_ambos_disponibles(self):
        # Ambos pueden reservar el mismo horario porque cada negocio es independiente
        r_a = appointments.create_appointment(
            "Cliente A", "3838439222", "Corte", self.valid_date, "09:00", business_id=1
        )
        r_b = appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "09:00", business_id=2
        )
        self.assertTrue(r_a["success"])
        self.assertTrue(r_b["success"])


# ============================================================
# ADMIN STATUS ISOLATION
# ============================================================

class TestAdminStatusIsolation(BaseAppointmentIsolationTest):

    def test_update_status_de_a_no_afecta_turno_de_b(self):
        turno_b = appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "09:00", business_id=2
        )["appointment_id"]
        actualizado = database.update_appointment_status_scoped(
            turno_b, "cancelled", 1
        )
        self.assertFalse(actualizado)
        estado = self._query(
            "SELECT status FROM appointments WHERE id = ?", (turno_b,)
        )[0]["status"]
        self.assertEqual(estado, "confirmed")


# ============================================================
# CONFIG ISOLATION
# ============================================================

class TestConfigIsolation(BaseAppointmentIsolationTest):

    def test_update_business_settings_de_a_no_afecta_config_de_b(self):
        # A actúa sobre su propia config (business_id=1)
        database.update_business_settings_scoped(
            1, "El Corte Renovado", "Barberia", "EC", "desc", "UTC"
        )
        config_b = database.get_business_settings_scoped(2)
        # B no tiene su propia fila de config -> devuelve None (no se crea una)
        # El punto: A no actualizó la fila de B
        filas_b = self._query(
            "SELECT business_name FROM business_settings WHERE business_id = 2"
        )
        self.assertEqual(len(filas_b), 0)


# ============================================================
# CLIENT-INJECTED business_id CANNOT ESCAPE CONTEXT (HTTP)
# ============================================================

class TestNoClientEscapesContext(BaseAppointmentIsolationTest):

    def test_business_id_inyectado_no_permite_ver_turnos_de_b(self):
        appointments.create_appointment(
            "Cliente B", "3838439333", "Corte B", self.valid_date, "09:00", business_id=2
        )
        self.login(1)
        # Cliente A intenta consultar turnos con business_id=2 en query string
        response = self.client.get(
            f"/api/turnos?nombre=Cliente+B&telefono=3838439333&business_id=2&business=2"
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        # No debe ver el turno de B porque el backend usa el contexto de A (id=1)
        self.assertEqual(data["turnos"], [])


if __name__ == "__main__":
    unittest.main()
