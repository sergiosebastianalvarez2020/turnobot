"""Tests de la verificación de backup (scripts/verify_backup.py).

Demuestran que:
    * un backup sano se verifica OK;
    * la base viva NO se modifica (solo lectura);
    * se detectan backups corruptos, violaciones de FK y desajustes de schema;
    * los conteos respetan la semántica de snapshot (backup <= live).
"""

import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import verify_backup as vb


def build_db(path, schema=22, businesses=1, settings=1, appointments=3, fk_violation=False):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE businesses (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE business_settings (
                business_id INTEGER PRIMARY KEY,
                notifications_enabled INTEGER DEFAULT 0
            );
            CREATE TABLE appointments (
                id INTEGER PRIMARY KEY,
                business_id INTEGER,
                status TEXT,
                FOREIGN KEY (business_id) REFERENCES businesses(id)
            );
            """
        )
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, ?)", (schema,))
        for business_id in range(1, businesses + 1):
            conn.execute("INSERT INTO businesses (id, name) VALUES (?, ?)", (business_id, f"b{business_id}"))
        for business_id in range(1, settings + 1):
            conn.execute(
                "INSERT INTO business_settings (business_id, notifications_enabled) VALUES (?, 0)",
                (business_id,),
            )
        for appointment_id in range(1, appointments + 1):
            business_id = 999 if fk_violation else ((appointment_id % businesses) + 1)
            conn.execute(
                "INSERT INTO appointments (id, business_id, status) VALUES (?, ?, 'confirmed')",
                (appointment_id, business_id),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(live_path, backup_path):
    Path(backup_path).write_bytes(Path(live_path).read_bytes())
    return backup_path


class TestVerifyBackup(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.live = self.dir / "appointments.db"
        self.backup = self.dir / "appointments-20260917-033000.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_healthy_backup_verifies_ok(self):
        build_db(self.live)
        snapshot(self.live, self.backup)
        ok, checks = vb.verify_backup(self.backup, self.live)
        self.assertTrue(ok, checks)
        self.assertTrue(all(check[1] for check in checks), checks)

    def test_live_database_is_not_modified(self):
        build_db(self.live)
        snapshot(self.live, self.backup)
        before_hash = sha256(self.live)
        before_files = sorted(p.name for p in self.dir.iterdir())

        vb.verify_backup(self.backup, self.live)
        vb.main(["--backups-dir", str(self.dir), "--live-db", str(self.live)])

        self.assertEqual(sha256(self.live), before_hash)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before_files)

    def test_backup_older_than_live_is_accepted(self):
        build_db(self.live, appointments=5)
        build_db(self.backup, appointments=3)
        ok, checks = vb.verify_backup(self.backup, self.live)
        self.assertTrue(ok, checks)

    def test_backup_with_more_rows_than_live_fails(self):
        build_db(self.live, appointments=3)
        build_db(self.backup, appointments=5)
        ok, checks = vb.verify_backup(self.backup, self.live)
        self.assertFalse(ok)
        self.assertIn(("count:appointments", False, "backup=5 live=3 (monotonic)"), checks)

    def test_exact_count_mismatch_fails(self):
        build_db(self.live, businesses=2, settings=2)
        build_db(self.backup, businesses=1, settings=1)
        ok, _ = vb.verify_backup(self.backup, self.live)
        self.assertFalse(ok)

    def test_schema_version_mismatch_fails(self):
        build_db(self.live, schema=22)
        build_db(self.backup, schema=21)
        ok, _ = vb.verify_backup(self.backup, self.live)
        self.assertFalse(ok)

    def test_corrupt_backup_fails_gracefully(self):
        build_db(self.live)
        Path(self.backup).write_bytes(b"esto no es una base sqlite")
        ok, checks = vb.verify_backup(self.backup, self.live)
        self.assertFalse(ok)
        self.assertTrue(any(name == "backup_readable" and not passed for name, passed, _ in checks), checks)

    def test_foreign_key_violation_fails(self):
        build_db(self.live)
        build_db(self.backup, fk_violation=True)
        ok, _ = vb.verify_backup(self.backup, self.live)
        self.assertFalse(ok)

    def test_find_latest_backup_picks_newest_and_ignores_manual(self):
        (self.dir / "appointments-20260908-022711.db").write_bytes(b"x")
        (self.dir / "appointments-20260917-034406.db").write_bytes(b"x")
        (self.dir / "appointments-20260901-010101.db-wal").write_bytes(b"x")
        (self.dir / "appointments-pre-p33-20260917-021445.db").write_bytes(b"x")
        latest = vb.find_latest_backup(self.dir)
        self.assertEqual(Path(latest).name, "appointments-20260917-034406.db")

    def test_find_latest_backup_returns_none_without_candidates(self):
        (self.dir / "appointments-pre-p33-20260917-021445.db").write_bytes(b"x")
        self.assertIsNone(vb.find_latest_backup(self.dir))


if __name__ == "__main__":
    unittest.main()
