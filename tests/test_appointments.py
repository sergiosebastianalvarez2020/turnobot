import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import database.database as database
from services import appointments


class TestReservaDisponible(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_reserva_en_horario_disponible(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00"
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["appointment_id"])


class TestReservaOcupada(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_reserva_en_horario_ocupado(self):
        appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00"
        )
        result = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "09:00"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")


class TestDomingoCerrado(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_reserva_en_domingo(self):
        sunday = datetime.now().date()
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", sunday.isoformat(), "09:00"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "closed_day")


class TestFechaInvalida(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_reserva_con_fecha_mal_formateada(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", "2025/08/18", "09:00"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_date")

    def test_reserva_con_fecha_pasada(self):
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", yesterday, "09:00"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "past_date")


class TestCancelacionTelefonoCorrecto(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_cancelar_con_telefono_correcto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00"
        )
        appointment_id = result["appointment_id"]
        self.assertTrue(appointments.cancel_appointment(appointment_id, "3838439222"))


class TestCancelacionTelefonoIncorrecto(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_cancelar_con_telefono_incorrecto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00"
        )
        appointment_id = result["appointment_id"]
        self.assertFalse(appointments.cancel_appointment(appointment_id, "9999999999"))


class TestReprogramacionHorarioOcupado(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_reprogramar_a_horario_ocupado(self):
        appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00"
        )
        result2 = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "15:00"
        )
        appointment_id = result2["appointment_id"]
        result = appointments.reschedule_appointment(
            appointment_id, self.valid_date, "09:00", "3838439333"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")


class TestDobleReservaSimultanea(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date()
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_doble_reserva_mismo_horario(self):
        result1 = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00"
        )
        result2 = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "09:00"
        )
        self.assertTrue(result1["success"])
        self.assertFalse(result2["success"])
        self.assertEqual(result2["reason"], "occupied")


if __name__ == "__main__":
    unittest.main()
