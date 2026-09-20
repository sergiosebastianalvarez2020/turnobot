"""
Tests de seguridad para operaciones de clientes.
Verifican que un cliente no pueda operar sobre turnos ajenos.
"""

import unittest
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

import database.database as database
from services import appointments


class SecurityOperationsTests(unittest.TestCase):
    """Tests de seguridad para operaciones de clientes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        # Configurar negocio y servicios
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO businesses (id, name, slug) VALUES (1, 'Test', 'test')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO business_settings (business_id, business_name, timezone) VALUES (1, 'Test', 'UTC')"
            )
            conn.execute(
                "INSERT INTO services (business_id, name, price, duration, active) VALUES (1, 'Corte', 1000, 30, 1)"
            )
            # Insertar horarios abiertos para TODOS los dias para evitar
            # que la prueba dependa del dia de la semana en que se ejecuta.
            for day in range(7):
                conn.execute(
                    "INSERT OR REPLACE INTO weekly_schedules "
                    "(business_id, day_of_week, is_open, morning_start, morning_end) "
                    "VALUES (1, ?, 1, '09:00', '18:00')",
                    (day,),
                )
            conn.commit()
        finally:
            conn.close()

        # Usar fecha futura para evitar errores de fecha pasada
        self.future_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _create_appointment(self, customer_name, phone, management_token, appointment_time="10:00"):
        """Helper para crear un turno y obtener su management_token."""
        result = appointments.create_appointment(
            customer_name=customer_name,
            phone=phone,
            service="Corte",
            appointment_date=self.future_date,
            appointment_time=appointment_time,
            business_id=1,
        )
        self.assertTrue(result["success"])
        return result["appointment_id"], result["management_token"]

    def test_cancel_own_appointment_with_token(self):
        """Cliente puede cancelar su propio turno con token válido."""
        appointment_id, token = self._create_appointment("Juan Pérez", "+5491112345678", "token123")

        result = appointments.cancel_appointment(
            appointment_id,
            "+5491112345678",
            1,
            token,
            "Juan Pérez",
        )
        self.assertTrue(result)

    def test_cancel_other_client_appointment_rejected(self):
        """Cliente A no puede cancelar turno de Cliente B."""
        appointment_id_a, token_a = self._create_appointment("Juan Pérez", "+5491112345678", "token_a")
        _, token_b = self._create_appointment("María López", "+5491112345679", "token_b", appointment_time="11:00")

        result = appointments.cancel_appointment(
            appointment_id_a,
            "+5491112345679",
            1,
            "token_b",
            "María López",
        )
        self.assertFalse(result)

    def test_cancel_with_wrong_token_rejected(self):
        """Cancelación con token incorrecto es rechazada."""
        appointment_id, token = self._create_appointment("Juan Pérez", "+5491112345678", "token_valido")

        result = appointments.cancel_appointment(
            appointment_id,
            "+5491112345678",
            1,
            "token_invalido",
            "Juan Pérez",
        )
        self.assertFalse(result)

    def test_cancel_without_token_rejected(self):
        """Cancelación sin token es rechazada."""
        appointment_id, token = self._create_appointment("Juan Pérez", "+5491112345678", "token_valido")

        result = appointments.cancel_appointment(
            appointment_id,
            "+5491112345678",
            1,
            None,
            "Juan Pérez",
        )
        self.assertFalse(result)

    def test_reschedule_own_appointment_with_token(self):
        """Cliente puede reprogramar su propio turno con token válido."""
        appointment_id, token = self._create_appointment("Juan Pérez", "+5491112345678", "token123")

        from services import appointments
        result = appointments.reschedule_appointment(
            appointment_id=appointment_id,
            new_date=self.future_date,
            new_time="11:00",
            phone="+5491112345678",
            business_id=1,
            management_token=token,
            customer_name="Juan Pérez",
        )
        self.assertTrue(result["success"])

    def test_reschedule_other_client_appointment_rejected(self):
        """Cliente A no puede reprogramar turno de Cliente B."""
        appointment_id_a, token_a = self._create_appointment("Juan Pérez", "+5491112345678", "token_a")
        appointment_id_b, token_b = self._create_appointment("María López", "+5491112345679", "token_b", appointment_time="11:00")

        from services import appointments
        result = appointments.reschedule_appointment(
            appointment_id=appointment_id_a,
            new_date=self.future_date,
            new_time="11:00",
            phone="+5491112345679",
            business_id=1,
            management_token=token_b,
            customer_name="María López",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")

    def test_reschedule_with_wrong_token_rejected(self):
        """Reprogramación con token incorrecto es rechazada."""
        appointment_id, token = self._create_appointment("Juan Pérez", "+5491112345678", "token_valido")

        from services import appointments
        result = appointments.reschedule_appointment(
            appointment_id=appointment_id,
            new_date="2025-01-15",
            new_time="11:00",
            phone="+5491112345678",
            business_id=1,
            management_token="token_invalido",
            customer_name="Juan Pérez",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")

    def test_reschedule_without_token_rejected(self):
        """Reprogramación sin token es rechazada."""
        appointment_id, token = self._create_appointment("Juan Pérez", "+5491112345678", "token_valido")

        from services import appointments
        result = appointments.reschedule_appointment(
            appointment_id=appointment_id,
            new_date="2025-01-15",
            new_time="11:00",
            phone="+5491112345678",
            business_id=1,
            management_token=None,
            customer_name="Juan Pérez",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")

    def test_cross_tenant_cancel_rejected(self):
        """Cliente de negocio A no puede cancelar turno de negocio B."""
        conn = __import__('database.database', fromlist=['get_connection']).get_connection()
        try:
            conn.execute(
                "INSERT INTO businesses (id, name, slug) VALUES (2, 'Otro', 'otro')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO business_settings (business_id, business_name, timezone) VALUES (2, 'Otro', 'UTC')"
            )
            conn.execute(
                "INSERT INTO services (business_id, name, price, duration, active) VALUES (2, 'Corte', 1000, 30, 1)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO weekly_schedules (business_id, day_of_week, is_open, morning_start, morning_end) VALUES (2, 0, 1, '09:00', '18:00')"
            )
            conn.commit()
        finally:
            conn.close()

        conn = __import__('database.database', fromlist=['get_connection']).get_connection()
        try:
            conn.execute(
                "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time, appointment_end, duration, business_id, status, management_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("Cliente B", "+5491199999999", "Corte", "2025-01-15", "10:00", "10:30", 30, 2, "confirmed", "hash_b")
            )
            conn.commit()
            appt_b_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

        from services import appointments
        result = appointments.cancel_appointment(
            appointment_id=appt_b_id,
            phone="+5491199999999",
            business_id=1,
            management_token="token_a",
            customer_name="Cliente A",
        )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()