"""Verifica la migración 023_appointment_idempotency_key.sql.

El caso que importa es el de PRODUCCIÓN: una base que ya tiene turnos created
sin la columna debe migrar sin perder filas y con `idempotency_key` en NULL, y
recién ahí debe quedar el índice único parcial.

Si la migración no fuera aplicable sobre una base pre-023, TODOS los
`create_appointment` de producción empezarían a fallar al insertar la columna
inexistente, así que el test reproduce ese upgrade real.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.database as database

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"
NUEVA = "023_appointment_idempotency_key.sql"


class TestAppointmentIdempotencyKeyMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "appointments.db"
        self.migrations_dir = self.root / "migrations"
        self.migrations_dir.mkdir()
        self.original_database_path = database.DATABASE_PATH
        self.original_migrations_dir = database.MIGRATIONS_DIR
        database.DATABASE_PATH = self.database_path
        database.MIGRATIONS_DIR = self.migrations_dir

        # Override backend to SQLite for this legacy migration test
        self._original_db_backend = os.environ.get("DB_BACKEND")
        self._original_db_url = os.environ.get("DATABASE_URL")
        os.environ["DB_BACKEND"] = "sqlite"
        if "DATABASE_URL" in os.environ:
            del os.environ["DATABASE_URL"]

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        database.MIGRATIONS_DIR = self.original_migrations_dir
        # Restore original backend env vars
        if self._original_db_backend is not None:
            os.environ["DB_BACKEND"] = self._original_db_backend
        else:
            os.environ.pop("DB_BACKEND", None)
        if self._original_db_url is not None:
            os.environ["DATABASE_URL"] = self._original_db_url
        else:
            os.environ.pop("DATABASE_URL", None)
        self.temp_dir.cleanup()

    def _copiar_hasta(self, incluir_023):
        for origen in sorted(MIGRATIONS.glob("*.sql")):
            if origen.name == NUEVA and not incluir_023:
                continue
            shutil.copy(origen, self.migrations_dir / origen.name)

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_upgrade_de_una_base_pre_023(self):
        # Base legada: todo el schema salvo la 023.
        self._copiar_hasta(incluir_023=False)
        database.init_database()

        connection = self.connect()
        columnas = {row["name"] for row in connection.execute("PRAGMA table_info(appointments)")}
        self.assertNotIn("idempotency_key", columnas)

        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, "
            "appointment_time, appointment_end, duration, status, business_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', 1)",
            ("Ana", "123456789", "Corte", "2099-01-01", "09:00", "09:30", 30),
        )
        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, "
            "appointment_time, appointment_end, duration, status, business_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', 1)",
            ("Bruno", "987654321", "Corte", "2099-01-01", "11:00", "11:30", 30),
        )
        connection.commit()
        connection.close()

        # Upgrade real: aparece la 023.
        shutil.copy(MIGRATIONS / NUEVA, self.migrations_dir / NUEVA)
        database.init_database()

        connection = self.connect()
        self.assertEqual(
            connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0], 23
        )

        columnas = {row["name"] for row in connection.execute("PRAGMA table_info(appointments)")}
        self.assertIn("idempotency_key", columnas)

        # Las filas preexistentes se conservan y quedan sin clave.
        filas = connection.execute(
            "SELECT customer_name, idempotency_key FROM appointments ORDER BY id"
        ).fetchall()
        self.assertEqual(len(filas), 2)
        self.assertTrue(all(fila["idempotency_key"] is None for fila in filas))
        self.assertEqual([f["customer_name"] for f in filas], ["Ana", "Bruno"])

        indices = {row[1] for row in connection.execute("PRAGMA index_list(appointments)")}
        self.assertIn("unique_appointment_idempotency_key", indices)
        self.assertIn("unique_confirmed_appointment_slot", indices)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        connection.close()

    def test_el_indice_es_parcial_y_rechaza_claves_repetidas(self):
        self._copiar_hasta(incluir_023=True)
        database.init_database()

        connection = self.connect()
        indexes = {
            row[1]: row[4]
            for row in connection.execute("PRAGMA index_list(appointments)").fetchall()
        }
        self.assertEqual(indexes["unique_appointment_idempotency_key"], 1, "debe ser UNIQUE")

        # NULL no viola el índice: los turnos sin clave conviven.
        for hora in ("09:00", "11:00"):
            connection.execute(
                "INSERT INTO appointments (customer_name, phone, service, appointment_date, "
                "appointment_time, appointment_end, duration, status, business_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'cancelled', 1)",
                (f"Cliente {hora}", "123456789", "Corte", "2099-01-01", hora, "11:30", 30),
            )

        # Misma clave en el mismo negocio: violaci��n.
        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, "
            "appointment_time, appointment_end, duration, status, business_id, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'cancelled', 1, ?)",
            ("Uno", "123456789", "Corte", "2099-01-01", "13:00", "13:30", 30, "clave-x"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO appointments (customer_name, phone, service, appointment_date, "
                "appointment_time, appointment_end, duration, status, business_id, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'cancelled', 1, ?)",
                ("Dos", "123456789", "Corte", "2099-01-01", "15:00", "15:30", 30, "clave-x"),
            )
        connection.rollback()

        # Misma clave en OTRO negocio: permitido (el UNIQUE es por negocio).
        connection.execute("INSERT INTO businesses (id, name, slug) VALUES (2, 'Otro', 'otro')")
        connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date, "
            "appointment_time, appointment_end, duration, status, business_id, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'cancelled', 2, ?)",
            ("Tres", "123456789", "Corte", "2099-01-01", "13:00", "13:30", 30, "clave-x"),
        )
        connection.commit()
        connection.close()


if __name__ == "__main__":
    unittest.main()
