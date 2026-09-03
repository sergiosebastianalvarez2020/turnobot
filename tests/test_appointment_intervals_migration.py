import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.database as database


ROOT = Path(__file__).resolve().parent.parent


class TestAppointmentIntervalsMigration(unittest.TestCase):
    """Verifica que 004_appointment_intervals.sql respeta el backfill:
    - servicios conocidos: duration y appointment_end correctos;
    - servicio inexistente: conserva el turno usando el fallback de
      configuracion (business_settings.slot_duration);"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "appointments.db"
        self.migrations_dir = self.root / "migrations"
        self.migrations_dir.mkdir()
        for name in ("001_initial.sql", "002_business_configuration.sql", "003_businesses.sql"):
            shutil.copy(ROOT / "migrations" / name, self.migrations_dir / name)
        self.original_database_path = database.DATABASE_PATH
        self.original_migrations_dir = database.MIGRATIONS_DIR
        database.DATABASE_PATH = self.database_path
        database.MIGRATIONS_DIR = self.migrations_dir

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        database.MIGRATIONS_DIR = self.original_migrations_dir
        self.temp_dir.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_backfill_de_duracion_y_appointment_end(self):
        # Aplicar hasta la migracion 003 (schema legacy)
        database.init_database()
        connection = self.connect()
        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time) VALUES (?, ?, ?, ?, ?)",
            ("Ana", "123456789", "Corte", "2099-01-01", "09:00"),
        )
        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time) VALUES (?, ?, ?, ?, ?)",
            ("Bruno", "987654321", "Corte + barba", "2099-01-01", "10:00"),
        )
        # Servicio inexistente en el negocio: debe conservarse con fallback
        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time) VALUES (?, ?, ?, ?, ?)",
            ("Desconocido", "555555555", "Servicio fantasma", "2099-01-01", "11:00"),
        )
        connection.commit()
        connection.close()

        # Aplicar la migracion 004
        shutil.copy(ROOT / "migrations" / "004_appointment_intervals.sql", self.migrations_dir / "004_appointment_intervals.sql")
        database.init_database()

        connection = self.connect()
        self.assertEqual(
            connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0],
            4,
        )

        rows = {
            row["service"]: row
            for row in connection.execute(
                "SELECT service, appointment_time, appointment_end, duration FROM appointments ORDER BY id"
            ).fetchall()
        }

        # Corte = 30 -> 09:00 - 09:30
        self.assertEqual(rows["Corte"]["duration"], 30)
        self.assertEqual(rows["Corte"]["appointment_end"], "09:30")
        # Corte + barba = 50 -> 10:00 - 10:50
        self.assertEqual(rows["Corte + barba"]["duration"], 50)
        self.assertEqual(rows["Corte + barba"]["appointment_end"], "10:50")
        # Servicio fantasma -> fallback business_settings.slot_duration (60)
        self.assertEqual(rows["Servicio fantasma"]["duration"], 60)
        self.assertEqual(rows["Servicio fantasma"]["appointment_end"], "12:00")

        # Se conservan todos los turnos y el UNIQUE parcial sigue presente
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0],
            3,
        )
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(appointments)")}
        self.assertIn("unique_confirmed_appointment_slot", indexes)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        connection.close()


if __name__ == "__main__":
    unittest.main()