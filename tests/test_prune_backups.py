"""Tests de la retención de backups automáticos (scripts/prune_backups.py).

Verifican la política (7 diarios / 4 semanales / 6 mensuales) para ambos
formatos gestionados (``appointments-*.db`` y ``turnobot-pg-*.dump``), el
aislamiento de la retención por formato, la protección del backup más reciente
y del día en curso, la preservación de archivos manuales/no gestionados, el
límite MAX_DELETIONS y que ``--dry-run`` NUNCA elimina archivos.
"""

import os
import sys
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import prune_backups


def make_backup(directory, moment, suffix=""):
    name = f"appointments-{moment:%Y%m%d-%H%M%S}{suffix}.db"
    (Path(directory) / name).write_bytes(b"backup")
    return name


def make_pg_backup(directory, moment, suffix=""):
    name = f"turnobot-pg-{moment:%Y%m%d-%H%M%S}{suffix}.dump"
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


class TestPrunePostgres(unittest.TestCase):
    """La misma política sobre dumps ``turnobot-pg-YYYYMMDD-HHMMSS.dump``."""

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

    def test_few_pg_dumps_are_all_kept(self):
        make_pg_backup(self.dir, datetime(2026, 9, 17, 3, 30))
        make_pg_backup(self.dir, datetime(2026, 9, 16, 3, 30))
        result = prune_backups.plan(self.dir, now=self.now)
        self.assertEqual(result["delete"], [])
        self.assertEqual(len(result["keep"]), 2)

    def test_pg_daily_policy_keeps_only_last_seven_days(self):
        names = []
        for day in range(8, 18):
            names.append(make_pg_backup(self.dir, datetime(2026, 9, day, 10, 0, 0)))
        result = self._plan(daily_keep=7)
        kept = [n for n in names if n in result["keep"]]
        deleted = [n for n in names if n in result["delete"]]
        self.assertEqual(len(kept), 7)
        self.assertEqual(len(deleted), 3)
        self.assertEqual(deleted, names[:3])

    def test_pg_weekly_policy_keeps_last_four_weeks(self):
        names = [
            make_pg_backup(self.dir, datetime(2026, 8, 10, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 8, 17, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 8, 24, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 8, 31, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 9, 7, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 9, 14, 3, 30)),
        ]
        result = self._plan(weekly_keep=4)
        deleted = [n for n in names if n in result["delete"]]
        self.assertEqual(deleted, names[:2])

    def test_pg_monthly_policy_keeps_last_six_months(self):
        names = [
            make_pg_backup(self.dir, datetime(2026, 2, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 3, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 4, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 5, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 6, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 7, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 8, 1, 3, 30)),
            make_pg_backup(self.dir, datetime(2026, 9, 1, 3, 30)),
        ]
        result = self._plan(monthly_keep=6)
        deleted = [n for n in names if n in result["delete"]]
        self.assertEqual(deleted, names[:2])

    def test_newest_pg_dump_and_current_day_are_kept(self):
        old = make_pg_backup(self.dir, datetime(2026, 1, 1, 3, 30))
        today = make_pg_backup(self.dir, datetime(2026, 9, 17, 3, 30))
        result = self._plan()
        self.assertIn(today, result["keep"])
        self.assertNotIn(today, result["delete"])
        self.assertIn(old, result["delete"])

    def test_pg_near_miss_names_are_preserved(self):
        valid = make_pg_backup(self.dir, datetime(2026, 9, 17, 3, 44, 6))
        near_miss = [
            "turnobot-pg-20260917-034406.dump-wal",
            "turnobot-pg-pre-p33-20260917-034406.dump",
            "turnobot-pg-20260917.dump",
            "turnobot-pg-20260917-034406.txt",
            "turnobot-pg-20260230-034406.dump",
            "turnobot-pg-20260917-034406",
            "appointments-20260917-034406.dump",
            "turnobot-sqlite-20260917-034406.dump",
            "notas.txt",
        ]
        for name in near_miss:
            (self.dir / name).write_bytes(b"x")
        result = self._plan()
        for name in near_miss:
            self.assertIn(name, result["preserved"])
            self.assertNotIn(name, result["delete"])
        self.assertIn(valid, result["keep"])
        self.assertEqual(result["delete"], [])

    def test_parse_timestamp_accepts_pg_names(self):
        self.assertIsNotNone(prune_backups.parse_timestamp("turnobot-pg-20260917-034406.dump"))
        self.assertIsNone(prune_backups.parse_timestamp("turnobot-pg-20260917-034406.dump-wal"))
        self.assertIsNone(prune_backups.parse_timestamp("turnobot-pg-20260230-034406.dump"))


class TestPruneMixedFormats(unittest.TestCase):
    """La retención se calcula por formato: ningún formato desplaza al otro."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.now = datetime(2026, 9, 17, 12, 0, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        sqlite_names, pg_names = [], []
        for day in range(8, 18):
            moment = datetime(2026, 9, day, 3, 30)
            sqlite_names.append(make_backup(self.dir, moment))
            pg_names.append(make_pg_backup(self.dir, moment))
        return sqlite_names, pg_names

    def test_formats_keep_their_own_seven_days(self):
        sqlite_names, pg_names = self._seed()
        result = prune_backups.plan(self.dir, now=self.now)

        for names in (sqlite_names, pg_names):
            kept = [n for n in names if n in result["keep"]]
            deleted = [n for n in names if n in result["delete"]]
            self.assertEqual(len(kept), 7, names)
            self.assertEqual(deleted, names[:3], names)

        self.assertEqual(len(result["delete"]), 6)
        self.assertEqual(len(result["keep"]), 14)

    def test_unknown_files_still_preserved_in_mixed_dir(self):
        self._seed()
        unknown = [
            "turnobot-pg-20260917-021445-manual.dump",
            "appointments-pre-p33-20260917-021445.db",
            "notas.txt",
        ]
        for name in unknown:
            (self.dir / name).write_bytes(b"x")
        result = prune_backups.plan(self.dir, now=self.now)
        for name in unknown:
            self.assertIn(name, result["preserved"])
            self.assertNotIn(name, result["delete"])

    def test_missing_dir_returns_2(self):
        missing = self.dir / "no-existe"
        self.assertEqual(prune_backups.main(["--dir", str(missing)]), 2)


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

    def _seed_pg(self):
        names = []
        for day in range(8, 18):
            names.append(make_pg_backup(self.dir, datetime(2026, 9, day, 10, 0, 0)))
        return names

    def test_dry_run_never_deletes_pg_dumps(self):
        names = self._seed_pg()
        (self.dir / "turnobot-pg-20260917-021445-manual.dump").write_bytes(b"manual")
        before = sorted(p.name for p in self.dir.iterdir())
        rc = prune_backups.main(["--dir", str(self.dir)])
        after = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(rc, 0)
        self.assertEqual(before, after)
        self.assertEqual(len(after), len(names) + 1)

    def test_apply_deletes_only_planned_pg_dumps(self):
        names = self._seed_pg()
        manual = "turnobot-pg-20260917-021445-manual.dump"
        (self.dir / manual).write_bytes(b"manual")
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
        self.assertEqual(len(expected_delete), len(names) - 7)

    def test_apply_guard_rejects_unmanaged_names(self):
        (self.dir / "notas.txt").write_bytes(b"x")
        with self.assertRaises(RuntimeError):
            prune_backups.apply_plan(self.dir, {"delete": ["notas.txt"]})
        with self.assertRaises(RuntimeError):
            prune_backups.apply_plan(self.dir, {"delete": ["turnobot-pg-20260230-034406.dump"]})
        self.assertTrue((self.dir / "notas.txt").exists())


class TestPruneCliLimits(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_dir_keeps_and_deletes_nothing(self):
        rc = prune_backups.main(["--dir", str(self.dir)])
        self.assertEqual(rc, 0)

    def test_apply_aborts_beyond_max_deletions(self):
        count = prune_backups.MAX_DELETIONS + 50
        last_day = datetime.now().date() - timedelta(days=2)
        first_day = last_day - timedelta(days=count - 1)
        for offset in range(count):
            day = first_day + timedelta(days=offset)
            make_backup(self.dir, datetime.combine(day, time(3, 30)))

        plan_result = prune_backups.plan(self.dir)
        self.assertGreater(len(plan_result["delete"]), prune_backups.MAX_DELETIONS)

        before = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(prune_backups.main(["--dir", str(self.dir)]), 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

        self.assertEqual(prune_backups.main(["--dir", str(self.dir), "--apply"]), 3)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
