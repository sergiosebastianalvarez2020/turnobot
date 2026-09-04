import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.database as database


ROOT = Path(__file__).resolve().parent.parent


class TestMigrationAudit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "appointments.db"
        self.migrations_dir = self.root / "migrations"
        self.migrations_dir.mkdir()
        for name in ("001_initial.sql", "002_business_configuration.sql", "003_businesses.sql",
                     "004_appointment_intervals.sql", "005_users_roles_sessions.sql",
                     "006_appointment_management_tokens.sql", "007_multitenant_business_settings.sql",
                     "008_notifications.sql"):
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

    def test_migration_log_records_all_applied_migrations(self):
        database.init_database()
        connection = self.connect()
        rows = connection.execute(
            "SELECT version, name, applied_at FROM migration_log ORDER BY version"
        ).fetchall()
        connection.close()
        self.assertEqual(
            [row["version"] for row in rows],
            [1, 2, 3, 4, 5, 6, 7, 8],
        )
        self.assertEqual(rows[0]["name"], "001_initial.sql")
        self.assertEqual(rows[-1]["name"], "008_notifications.sql")
        self.assertEqual(
            database.get_applied_migrations(),
            [dict(r) for r in rows],
        )

    def test_migration_log_has_schema_and_is_idempotent(self):
        database.init_database()
        connection = self.connect()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(migration_log)")}
        self.assertIn("id", columns)
        self.assertIn("version", columns)
        self.assertIn("name", columns)
        self.assertIn("applied_at", columns)
        connection.close()

        database.init_database()
        rows = database.get_applied_migrations()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r["version"] for r in rows}), 8)

    def test_migration_log_is_unique_per_version(self):
        database.init_database()
        connection = self.connect()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO migration_log (version, name) VALUES (?, ?)", (3, "dup")
            )
        connection.rollback()
        connection.close()

    def test_reconciles_preexisting_database(self):
        # Base de datos que ya pasó la migración 003 (sin registro de auditoría).
        (self.migrations_dir / "004_appointment_intervals.sql").unlink()
        (self.migrations_dir / "005_users_roles_sessions.sql").unlink()
        (self.migrations_dir / "006_appointment_management_tokens.sql").unlink()
        (self.migrations_dir / "007_multitenant_business_settings.sql").unlink()
        (self.migrations_dir / "008_notifications.sql").unlink()
        database.init_database()
        connection = self.connect()
        self.assertEqual(
            connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0], 3
        )
        connection.close()

        # Se vuelven a exponer todas las migraciones. El registro debe reconciliar
        # las ya aplicadas (1..3) y luego aplicar las pendientes (4..8).
        for name in ("004_appointment_intervals.sql", "005_users_roles_sessions.sql",
                     "006_appointment_management_tokens.sql", "007_multitenant_business_settings.sql",
                     "008_notifications.sql"):
            shutil.copy(ROOT / "migrations" / name, self.migrations_dir / name)
        database.init_database()

        rows = database.get_applied_migrations()
        self.assertEqual(len(rows), 8)
        self.assertEqual([r["version"] for r in rows], [1, 2, 3, 4, 5, 6, 7, 8])
        connection = self.connect()
        self.assertEqual(
            connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0], 8
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
