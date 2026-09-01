import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.database as database


ROOT = Path(__file__).resolve().parent.parent


class TestBusinessMigration(unittest.TestCase):
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
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def snapshot(self, connection):
        result = {}
        for table in ("business_settings", "services", "weekly_schedules", "appointments"):
            result[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
        return result

    def test_migrates_new_database(self):
        database.init_database()
        connection = self.connect()
        self.assertEqual(connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0], 3)
        business = connection.execute("SELECT id, name, slug FROM businesses").fetchone()
        self.assertEqual(dict(business), {"id": 1, "name": "El Corte", "slug": "el-corte"})
        for table in ("business_settings", "services", "weekly_schedules", "appointments"):
            self.assertTrue(any(row[1] == "business_id" for row in connection.execute(f"PRAGMA table_info({table})")))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM services").fetchone()[0], 3)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM weekly_schedules").fetchone()[0], 7)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM services WHERE business_id = 1").fetchone()[0], 3)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM weekly_schedules WHERE business_id = 1").fetchone()[0], 7)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def test_migrates_existing_database_without_changing_data(self):
        (self.migrations_dir / "003_businesses.sql").unlink()
        database.init_database()
        connection = self.connect()
        connection.execute("INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time) VALUES (?, ?, ?, ?, ?)", ("Ana", "123456789", "Corte", "2099-01-01", "09:00"))
        connection.commit()
        before = self.snapshot(connection)
        connection.close()

        shutil.copy(ROOT / "migrations" / "003_businesses.sql", self.migrations_dir / "003_businesses.sql")
        database.init_database()

        connection = self.connect()
        after = self.snapshot(connection)
        self.assertEqual({table: len(rows) for table, rows in before.items()}, {table: len(rows) for table, rows in after.items()})
        for table in before:
            for old, new in zip(before[table], after[table]):
                for key, value in old.items():
                    self.assertEqual(new[key], value)
                self.assertEqual(new["business_id"], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM businesses").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def test_new_constraints_and_indexes(self):
        database.init_database()
        connection = self.connect()
        connection.execute("INSERT INTO businesses (name, slug) VALUES (?, ?)", ("Business B", "business-b"))
        business_b = connection.execute("SELECT id FROM businesses WHERE slug = 'business-b'").fetchone()[0]
        connection.execute("INSERT INTO weekly_schedules (day_of_week, business_id) VALUES (?, ?)", (0, business_b))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO weekly_schedules (day_of_week, business_id) VALUES (?, ?)", (0, business_b))
        connection.execute("INSERT INTO appointments (customer_name, service, appointment_date, appointment_time, business_id) VALUES (?, ?, ?, ?, ?)", ("A", "Corte", "2099-01-02", "09:00", 1))
        connection.execute("INSERT INTO appointments (customer_name, service, appointment_date, appointment_time, business_id) VALUES (?, ?, ?, ?, ?)", ("B", "Corte", "2099-01-02", "09:00", business_b))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO appointments (customer_name, service, appointment_date, appointment_time, business_id) VALUES (?, ?, ?, ?, ?)", ("A2", "Corte", "2099-01-02", "09:00", 1))
        connection.execute("INSERT INTO appointments (customer_name, service, appointment_date, appointment_time, status, business_id) VALUES (?, ?, ?, ?, 'cancelled', ?)", ("A3", "Corte", "2099-01-02", "09:00", 1))
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(appointments)")}
        self.assertIn("unique_confirmed_appointment_slot", indexes)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()


if __name__ == "__main__":
    unittest.main()
