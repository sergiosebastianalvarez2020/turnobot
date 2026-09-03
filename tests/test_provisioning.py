import tempfile
import unittest
from pathlib import Path

import database.database as database


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.tmp.cleanup()

    def test_creates_business_owner_settings_and_schedules_atomically(self):
        result = database.create_business_with_owner("Negocio Nuevo", "owner@example.com", "una-clave-segura")
        connection = database.get_connection()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM businesses WHERE id = ?", (result["business_id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT role_id FROM business_users WHERE user_id = ?", (result["user_id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM business_settings WHERE business_id = ?", (result["business_id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM weekly_schedules WHERE business_id = ?", (result["business_id"],)).fetchone()[0], 7)
        finally:
            connection.close()

    def test_slug_se_hace_unico_y_email_duplicado_hace_rollback(self):
        first = database.create_business_with_owner("Mi Negocio", "uno@example.com", "una-clave-segura")
        second = database.create_business_with_owner("Mi Negocio", "dos@example.com", "otra-clave-segura")
        self.assertNotEqual(first["slug"], second["slug"])
        with self.assertRaises(Exception):
            database.create_business_with_owner("Tercero", "uno@example.com", "tercera-clave")
        connection = database.get_connection()
        try:
            self.assertIsNone(connection.execute("SELECT 1 FROM businesses WHERE slug = 'tercero'").fetchone())
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
