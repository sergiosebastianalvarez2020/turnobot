import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import database.database as database
from services import appointments


class AppointmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self.next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def create(self, phone="111"):
        return appointments.create_appointment(
            "Ana Pérez", phone, "Corte", self.valid_date, "09:00"
        )

    def test_creates_a_valid_appointment(self):
        result = self.create()
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["appointment_id"])

    def test_rejects_an_occupied_slot(self):
        self.create()
        result = self.create(phone="222")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")

    def test_rejects_a_closed_day(self):
        sunday = datetime.now().date()
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        result = appointments.create_appointment(
            "Ana Pérez", "111", "Corte", sunday.isoformat(), "09:00"
        )
        self.assertEqual(result["reason"], "closed_day")

    def test_cancellation_requires_the_registered_phone(self):
        appointment_id = self.create()["appointment_id"]
        self.assertFalse(appointments.cancel_appointment(appointment_id, "999"))
        self.assertTrue(appointments.cancel_appointment(appointment_id, "111"))

    def test_longest_service_sets_a_safe_slot_interval(self):
        connection = database.get_connection()
        connection.execute("UPDATE services SET duration = 90 WHERE name = 'Corte'")
        connection.commit()
        connection.close()

        self.assertEqual(
            appointments.get_available_slots(self.valid_date)[:3],
            ["09:00", "10:30", "12:00"],
        )


if __name__ == "__main__":
    unittest.main()
