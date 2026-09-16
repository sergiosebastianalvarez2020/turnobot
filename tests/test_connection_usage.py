import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import database.database as database
from services import appointments

ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")


class _BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "connections.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def _count_connections(self, target):
        real_connect = sqlite3.connect
        counted = {"n": 0}

        def counting_connect(*args, **kwargs):
            counted["n"] += 1
            return real_connect(*args, **kwargs)

        with mock.patch("database.database.sqlite3.connect", side_effect=counting_connect):
            target()

        return counted["n"]


class TestAvailableTimesConnections(_BaseTestCase):
    def test_get_available_times_abre_una_sola_conexion(self):
        count = self._count_connections(
            lambda: appointments.get_available_times(self.valid_date, 1)
        )
        self.assertEqual(count, 1)


class TestCreateAppointmentConnections(_BaseTestCase):
    def test_create_appointment_abre_una_sola_conexion(self):
        count = self._count_connections(
            lambda: appointments.create_appointment(
                "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
            )
        )
        self.assertEqual(count, 1)


class TestRescheduleConnections(_BaseTestCase):
    def setUp(self):
        super().setUp()
        self.appointment = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.management_token = self.appointment["management_token"]

    def test_reschedule_appointment_abre_una_sola_conexion(self):
        next_date = (datetime.strptime(self.valid_date, "%Y-%m-%d") + timedelta(days=1)).isoformat()
        count = self._count_connections(
            lambda: appointments.reschedule_appointment(
                self.appointment["appointment_id"],
                next_date,
                "10:00",
                phone="3838439222",
                business_id=1,
                management_token=self.management_token,
            )
        )
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()