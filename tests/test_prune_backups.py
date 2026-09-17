"""Tests de la retención de backups automáticos (scripts/prune_backups.py).

Verifican la política (7 diarios / 4 semanales / 6 mensuales), la protección
del backup más reciente y del día en curso, la preservación de archivos
manuales/no gestionados y que ``--dry-run`` NUNCA elimina archivos.
"""

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import prune_backups


def make_backup(directory, moment, suffix=""):
    name = f"appointments-{moment:%Y%m%d-%H%M%S}{suffix}.db"
    (Path(directory) / name).write_bytes(b"backup")
    return name


class TestPrunePlan(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.now = datetime(2026, 9, 17, 12, 0, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, **kwargs):
        kwargs.setdefault("now", self.now)
        kwargs.setdefault("daily_keep", 0)
        kwargs.setdefault("weekly_keep", 0)
        kwargs.setdefault("monthly_keep", 0)
        return prune_backups.plan(self.dir, **kwargs)

    def test_few_backups_are_all_kept(self):
        make_backup(self.dir, datetime(2026, 9, 17, 3, 30))
        make_backup(self.dir, datetime(2026, 9, 16, 3, 30))
        result = prune_backups.plan(self.dir, now=self.now)
        self.assertEqual(result["delete"], [])
        self.assertEqual(len(result["keep"]), 2)

    def test_manual_and_unknown_files_are_preserved(self):
        make_backup(self.dir, datetime(2026, 9, 17, 3, 30))
        manual = [
            "appointments-20260908-023742-pre-migration.db",
            "appointments-pre-p33-20260917-021445.db",
            "appointments-pre-p33-20260917-021445.db-wal",
            "appointments-pre-p33-20260917-021445.db-shm",
            "notas.txt",
        ]
        for name in manual:
            (self.dir / name).write_bytes(b"x")
        result = self._plan()
        for name in manual:
            self.assertIn(name, result["preserved"])
            self.assertNotIn(name, result["delete"])
        self.assertEqual(result["delete"], [])

    def test_newest_backup_is_always_kept(self):
        for day in range(1, 21):
            make_backup(self.dir, datetime(2026, 8, day, 3, 30))
        newest = make_backup(self.dir, datetime(2026, 9, 17, 3, 30))
        result = self.plan_all_buckets()
        self.assertIn(newest, result["keep"])
        self.assertNotIn(newest, result["delete"])

    def plan_all_buckets(self):
        return prune_backups.plan(self.dir, now=self.now)

    def test_current_day_backups_are_kept(self):
        old = make_backup(self.dir, datetime(2026, 1, 1, 3, 30))
        today_a = make_backup(self.dir, datetime(2026, 9, 17, 1, 0, 0))
        today_b = make_backup(self.dir, datetime(2026, 9, 17, 3, 30, 0))
        result = self._plan()
        self.assertIn(today_a, result["keep"])
        self.assertIn(today_b, result["keep"])
        self.assertNotIn(today_a, result["delete"])
        self.assertNotIn(today_b, result["delete"])
        self.assertIn(old, result["delete"])

    def test_daily_policy_keeps_only_last_seven_days(self):
        names = []
        for day in range(8, 18):
            names.append(make_backup(self.dir, datetime(2026, 9, day, 10, 0, 0)))
        result = self._plan(daily_keep=7)
        kept = [n for n in names if n in result["keep"]]
        deleted = [n for n in names if n in result["delete"]]
        self.assertEqual(len(kept), 7)
        self.assertEqual(len(deleted), 3)
        self.assertEqual(deleted, names[:3])

    def test_weekly_policy_keeps_last_four_weeks(self):
        names = [
            make_backup(self.dir, datetime(2026, 8, 10, 3, 30)),
            make_backup(self.dir, datetime(2026, 8, 17, 3, 30)),
            make_backup(self.dir, datetime(2026, 8, 24, 3, 30)),
            make_backup(self.dir, datetime(2026, 8, 31, 3, 30)),
            make_backup(self.dir, datetime(2026, 9, 7, 3, 30)),
            make_backup(self.dir, datetime(2026, 9, 14, 3, 30)),
        ]
        result = self._plan(weekly_keep=4)
        deleted = [n for n in names if n in result["delete"]]
        self.assertEqual(deleted, names[:2])

    def test_monthly_policy_keeps_last_six_months(self):
        names = [
            make_backup(self.dir, datetime(2026, 2, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 3, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 4, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 5, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 6, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 7, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 8, 1, 3, 30)),
            make_backup(self.dir, datetime(2026, 9, 1, 3, 30)),
        ]
        result = self._plan(monthly_keep=6)
        deleted = [n for n in names if n in result["delete"]]
        self.assertEqual(deleted, names[:2])

    def test_symlink_is_preserved_not_deleted(self):
        target = self.dir / "real-target.db"
        target.write_bytes(b"x")
        link = self.dir / "appointments-20260101-000000.db"
        os.symlink(target, link)
        make_backup(self.dir, datetime(2026, 9, 17, 3, 30))
        result = self._plan()
        self.assertIn(link.name, result["preserved"])
        self.assertNotIn(link.name, result["delete"])

    def test_parse_timestamp_rejects_non_matching_names(self):
        self.assertIsNone(prune_backups.parse_timestamp("appointments-pre-p33-20260917-021445.db"))
        self.assertIsNone(prune_backups.parse_timestamp("appointments-20260917-034406.db-wal"))
        self.assertIsNone(prune_backups.parse_timestamp("otro.db"))
        self.assertIsNotNone(prune_backups.parse_timestamp("appointments-20260917-034406.db"))


class TestPruneApply(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        names = []
        for day in range(8, 18):
            names.append(make_backup(self.dir, datetime(2026, 9, day, 10, 0, 0)))
        manual = "appointments-pre-p33-20260917-021445.db"
        (self.dir / manual).write_bytes(b"manual")
        return names, manual

    def test_dry_run_never_deletes(self):
        names, manual = self._seed()
        before = sorted(p.name for p in self.dir.iterdir())
        rc = prune_backups.main(["--dir", str(self.dir)])
        after = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(rc, 0)
        self.assertEqual(before, after)
        self.assertEqual(len(after), len(names) + 1)
        self.assertIn(manual, after)

    def test_apply_deletes_only_planned_files(self):
        names, manual = self._seed()
        plan_result = prune_backups.plan(self.dir, now=datetime(2026, 9, 17, 12, 0, 0))
        expected_delete = set(plan_result["delete"])
        expected_keep = set(plan_result["keep"])
        rc = prune_backups.main(["--dir", str(self.dir), "--apply"])
        self.assertEqual(rc, 0)
        remaining = {p.name for p in self.dir.iterdir()}
        for name in expected_delete:
            self.assertNotIn(name, remaining)
        for name in expected_keep:
            self.assertIn(name, remaining)
        self.assertIn(manual, remaining)


if __name__ == "__main__":
    unittest.main()
