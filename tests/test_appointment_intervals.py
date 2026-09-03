import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import database.database as database
from services import appointments


def next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class BaseIntervalTest(unittest.TestCase):
    """Business 1 (El Corte) en base temporal. Se configura grilla fina
    (slot_duration=15) para poder expresar horarios como 09:15."""

    DATE = None

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        weekday = datetime.now().weekday()
        while weekday == 6:
            weekday = (weekday + 1) % 7
        self._execute(
            "UPDATE business_settings SET slot_duration = 15, break_between_slots = 0 WHERE business_id = 1"
        )
        self._execute(
            "UPDATE weekly_schedules SET is_open = 1, morning_start = '09:00', morning_end = '12:00', afternoon_start = NULL, afternoon_end = NULL WHERE business_id = 1 AND day_of_week = ?",
            (weekday,),
        )
        self._execute(
            "UPDATE services SET business_id = 1 WHERE id IN (1, 2, 3)"
        )
        # Servicios con duraciones variadas (misma empresa, negocio 1)
        self.services = {
            "S15": self._insert_service("S15", 15),
            "S20": self._insert_service("S20", 20),
            "S30": self._insert_service("S30", 30),
            "S50": self._insert_service("S50", 50),
            "S60": self._insert_service("S60", 60),
        }
        self.valid_date = next_open_day()
        BaseIntervalTest.DATE = self.valid_date

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

    def _insert_service(self, name, duration):
        connection = database.get_connection()
        try:
            cursor = connection.execute(
                "INSERT INTO services (business_id, name, price, duration, active) VALUES (1, ?, 1000, ?, 1)",
                (name, duration),
            )
            connection.commit()
            return cursor.lastrowid
        finally:
            connection.close()

    def _book(self, service_name, time, business_id=1, name="Cliente", phone="123456789"):
        return appointments.create_appointment(
            name, phone, service_name, self.valid_date, time, business_id
        )


class TestDurationAndEnd(BaseIntervalTest):

    def test_duracion_20(self):
        result = self._book("S20", "09:00")
        self.assertTrue(result["success"])
        row = self._query("SELECT duration, appointment_end FROM appointments WHERE id = ?", (result["appointment_id"],))[0]
        self.assertEqual(row["duration"], 20)
        self.assertEqual(row["appointment_end"], "09:20")

    def test_duracion_30(self):
        result = self._book("S30", "09:00")
        self.assertTrue(result["success"])
        row = self._query("SELECT duration, appointment_end FROM appointments WHERE id = ?", (result["appointment_id"],))[0]
        self.assertEqual(row["duration"], 30)
        self.assertEqual(row["appointment_end"], "09:30")

    def test_duracion_50(self):
        result = self._book("S50", "09:00")
        self.assertTrue(result["success"])
        row = self._query("SELECT duration, appointment_end FROM appointments WHERE id = ?", (result["appointment_id"],))[0]
        self.assertEqual(row["duration"], 50)
        self.assertEqual(row["appointment_end"], "09:50")

    def test_duracion_60(self):
        result = self._book("S60", "09:00")
        self.assertTrue(result["success"])
        row = self._query("SELECT duration, appointment_end FROM appointments WHERE id = ?", (result["appointment_id"],))[0]
        self.assertEqual(row["duration"], 60)
        self.assertEqual(row["appointment_end"], "10:00")


class TestOverlap(BaseIntervalTest):

    def setUp(self):
        super().setUp()
        self._book("S60", "09:00")  # ocupa 09:00-10:00

    def test_09_15_a_09_30_rechazar(self):
        result = self._book("S15" if "S15" in self.services else "S20", "09:15")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")

    def test_09_30_a_10_00_rechazar(self):
        result = self._book("S30", "09:30")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")

    def test_10_00_a_10_30_permitir(self):
        result = self._book("S30", "10:00")
        self.assertTrue(result["success"])


class TestCrossBusiness(BaseIntervalTest):

    def test_mismo_horario_distinto_negocio_permitido(self):
        self._execute("INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')")
        for day in range(7):
            self._execute(
                "INSERT INTO weekly_schedules (business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
                "VALUES (2, ?, 1, '09:00', '12:00', NULL, NULL)",
                (day,),
            )
        self._insert_business_b_service("S60 B", 60)

        a = self._book("S60", "09:00", business_id=1)
        b = self._book("S60 B", "09:00", business_id=2)
        self.assertTrue(a["success"])
        self.assertTrue(b["success"])

    def _insert_business_b_service(self, name, duration):
        connection = database.get_connection()
        try:
            cursor = connection.execute(
                "INSERT INTO services (business_id, name, price, duration, active) VALUES (2, ?, 1000, ?, 1)",
                (name, duration),
            )
            connection.commit()
            return cursor.lastrowid
        finally:
            connection.close()


class TestHistoricalDuration(BaseIntervalTest):

    def test_cambio_de_duracion_conserva_historia(self):
        result = self._book("S30", "09:00")
        self.assertTrue(result["success"])
        appointment_id = result["appointment_id"]

        # El admin aumenta la duración del servicio a 60
        self._execute(
            "UPDATE services SET duration = 60 WHERE id = ?",
            (self.services["S30"],),
        )

        row = self._query("SELECT duration, appointment_end FROM appointments WHERE id = ?", (appointment_id,))[0]
        self.assertEqual(row["duration"], 30)
        self.assertEqual(row["appointment_end"], "09:30")


class TestClosingTime(BaseIntervalTest):

    def test_termina_exacto_al_cierre_permitido(self):
        # Reconfigurar día: 09:00-10:00. Un servicio de 60 min que inicia 09:00
        # termina exactamente al cierre -> permitido.
        self._execute(
            "UPDATE weekly_schedules SET is_open = 1, morning_start = '09:00', morning_end = '10:00', afternoon_start = NULL, afternoon_end = NULL WHERE business_id = 1 AND day_of_week = ?",
            (datetime.fromisoformat(self.valid_date).weekday(),),
        )
        result = self._book("S60", "09:00")
        self.assertTrue(result["success"])
        row = self._query("SELECT appointment_end FROM appointments WHERE id = ?", (result["appointment_id"],))[0]
        self.assertEqual(row["appointment_end"], "10:00")

    def test_excede_el_cierre_rechazado(self):
        self._execute(
            "UPDATE weekly_schedules SET is_open = 1, morning_start = '09:00', morning_end = '10:00', afternoon_start = NULL, afternoon_end = NULL WHERE business_id = 1 AND day_of_week = ?",
            (datetime.fromisoformat(self.valid_date).weekday(),),
        )
        # Un servicio de 60 min que inicia 09:30 terminaría 10:30 > cierre.
        result = self._book("S60", "09:30")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_time")


class TestConcurrency(BaseIntervalTest):

    def test_dos_reservas_solapadas_simultaneas_solo_una_confirmada(self):
        barrier = threading.Barrier(2)
        results = []

        def book():
            barrier.wait()
            results.append(self._book("S30", "09:00"))

        threads = [threading.Thread(target=book) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r["success"] for r in results]
        self.assertEqual(successes.count(True), 1)
        self.assertEqual(len(results), 2)


class TestInvalidDuration(BaseIntervalTest):

    def test_duracion_cero_rechazada(self):
        self._execute(
            "UPDATE services SET duration = 0 WHERE id = ?",
            (self.services["S30"],),
        )
        result = self._book("S30", "09:00")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_duration")

    def test_duracion_negativa_rechazada(self):
        self._execute(
            "UPDATE services SET duration = -1 WHERE id = ?",
            (self.services["S30"],),
        )
        result = self._book("S30", "09:00")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_duration")

    def test_duracion_null_bloqueada_por_esquema(self):
        with self.assertRaises(Exception):
            self._execute(
                "INSERT INTO services (business_id, name, price, duration, active) VALUES (1, 'S NULL', 1000, NULL, 1)"
            )


if __name__ == "__main__":
    unittest.main()