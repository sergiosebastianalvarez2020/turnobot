"""Tests de backup/restore con modo WAL de SQLite.

Reproducen y previenen el defecto P.1: el backup basado en shutil.copy2
del archivo .db produce una copia incompleta cuando la base está en modo
WAL y los datos recientes residen en el archivo .db-wal.

Estos tests verifican la propiedad: "el backup representa consistentemente
el estado de la BD", sin acoplarse a una implementación concreta.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.database as database
from scripts.backup_database import backup_database


ROOT = Path(__file__).resolve().parent.parent


class TestBackupWalConsistency(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.root_dir / "appointments.db"
        database.init_database()
        self._open_connections = []

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        for conn in self._open_connections:
            conn.close()
        self.temp_dir.cleanup()

    def _open_conn(self):
        conn = database.get_connection()
        self._open_connections.append(conn)
        return conn

    def _assert_backup_has_data(self, backup_path, expected_value):
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            backup_conn.row_factory = sqlite3.Row
            row = backup_conn.execute(
                "SELECT val_test FROM wal_markers WHERE id = ?", (1,)
            ).fetchone()
            self.assertIsNotNone(row, "La tabla wal_markers no existe en el backup")
            self.assertEqual(row["val_test"], expected_value)

            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok", "integrity_check falló en el backup")
        finally:
            backup_conn.close()

    def test_backup_includes_wal_only_data(self):
        """El backup debe incluir datos que sólo existen en el archivo WAL.

        Se escribe un marcador en la base con una conexión mantenida abierta
        (simulando una aplicación activa). El backup debe reflejar ese estado.
        """
        marker_value = "marker-123456"

        conn = self._open_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS wal_markers (id INTEGER PRIMARY KEY, val_test TEXT)")
        conn.execute("INSERT INTO wal_markers (id, val_test) VALUES (1, ?)", (marker_value,))
        conn.commit()

        # Mantener una conexión abierta: impide que SQLite haga checkpoint del
        # WAL al cerrar la conexión escritora, dejando los datos en el .db-wal.
        self._open_conn()

        backup_path = backup_database(destination=str(self.root_dir / "backups"))
        self.assertTrue(backup_path.exists(), "No se generó el archivo de backup")

        self._assert_backup_has_data(backup_path, marker_value)

    def test_backup_consistent_after_close(self):
        """El backup realizado con conexiones activas debe seguir siendo
        consistente e íntegro al abrirse de nuevo."""

        marker_value = "marker-abc"

        conn = self._open_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS wal_markers (id INTEGER PRIMARY KEY, val_test TEXT)")
        conn.execute("INSERT INTO wal_markers (id, val_test) VALUES (1, ?)", (marker_value,))
        conn.commit()

        keeper = self._open_conn()

        backup_path = backup_database(destination=str(self.root_dir / "backups"))

        keeper.close()
        self._open_connections.remove(keeper)
        conn.close()
        self._open_connections.remove(conn)

        backup_conn = sqlite3.connect(str(backup_path))
        try:
            backup_conn.row_factory = sqlite3.Row
            row = backup_conn.execute(
                "SELECT val_test FROM wal_markers WHERE id = ?", (1,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["val_test"], marker_value)
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")
        finally:
            backup_conn.close()

    def test_backup_restores_via_restore_script(self):
        """El backup generado debe poder restaurarse con el mecanismo
        actual de restore_database.py (shutil.copy2) y conservar los datos."""

        marker_value = "marker-restore-test"

        conn = self._open_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS wal_markers (id INTEGER PRIMARY KEY, val_test TEXT)")
        conn.execute("INSERT INTO wal_markers (id, val_test) VALUES (1, ?)", (marker_value,))
        conn.commit()

        keeper = self._open_conn()

        backup_path = backup_database(destination=str(self.root_dir / "backups"))

        keeper.close()
        self._open_connections.remove(keeper)
        conn.close()
        self._open_connections.remove(conn)

        # Simular restore: copiar el backup sobre una ruta destino
        restore_path = self.root_dir / "restored.db"
        shutil.copy2(backup_path, restore_path)

        restored_conn = sqlite3.connect(str(restore_path))
        try:
            restored_conn.row_factory = sqlite3.Row
            restored_conn.execute("PRAGMA journal_mode=WAL")
            row = restored_conn.execute(
                "SELECT val_test FROM wal_markers WHERE id = ?", (1,)
            ).fetchone()
            self.assertIsNotNone(row, "La tabla no existe tras restore")
            self.assertEqual(row["val_test"], marker_value)
            integrity = restored_conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")
        finally:
            restored_conn.close()

    def test_backup_filename_unchanged(self):
        """El backup debe conservar el naming actual de la aplicación."""
        import re

        backup_path = backup_database(destination=str(self.root_dir / "backups"))
        pattern = r"^appointments-\d{8}-\d{6}\.db$"
        self.assertRegex(backup_path.name, pattern,
                         "El nombre del backup no sigue el formato esperado")

    def test_backup_nonexistent_source_raises(self):
        """Si la base no existe, debe lanzar FileNotFoundError."""
        database.DATABASE_PATH = self.root_dir / "nonexistent.db"
        with self.assertRaises(FileNotFoundError):
            backup_database(destination=str(self.root_dir / "backups"))


if __name__ == "__main__":
    unittest.main()
