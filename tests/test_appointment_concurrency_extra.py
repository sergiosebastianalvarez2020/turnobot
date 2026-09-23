"""Casos adicionales de concurrencia en reservas (Extension 5).

Cubre escenarios concurrentes sobre el mismísimo CREATE que el test existente
(test_appointment_intervals.TestConcurrency) pero con matrices distintas:
slots no solapados, aislamiento por recurso y mezcla recurso+global.
"""

import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import database.database as database
from services import appointments


def _next_open_weekday():
    day = datetime.now().date() + timedelta(days=1)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


class _BaseConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self._tmp.name) / "appointments.db"
        database.init_database()

        weekday = datetime.now().weekday()
        while weekday == 6:
            weekday = (weekday + 1) % 7
        self._exec(
            "UPDATE business_settings SET slot_duration = 15, break_between_slots = 0 "
            "WHERE business_id = 1"
        )
        self._exec(
            "UPDATE weekly_schedules SET is_open = 1, morning_start = '09:00', "
            "morning_end = '12:00', afternoon_start = NULL, afternoon_end = NULL "
            "WHERE business_id = 1 AND day_of_week = ?",
            (weekday,),
        )
        self.valid_date = _next_open_weekday()

    def tearDown(self):
        database.DATABASE_PATH = self._original_database_path
        self._tmp.cleanup()

    def _exec(self, sql, params=()):
        connection = database.get_connection()
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def _query(self, sql, params=()):
        connection = database.get_connection()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def _book(self, time_, resource_id=None):
        return appointments.create_appointment(
            "Cliente", "123456789", "Corte", self.valid_date, time_, 1, resource_id=resource_id
        )

    def _run_threads(self, funcs):
        barrier = threading.Barrier(len(funcs))
        results = []

        def runner(fn):
            barrier.wait()
            results.append(fn())

        threads = [threading.Thread(target=runner, args=(fn,)) for fn in funcs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results


class TestConcurrentBookings(_BaseConcurrencyTest):
    def test_slots_distintos_simultaneos_ambas_exitosas(self):
        results = self._run_threads([lambda: self._book("09:00"), lambda: self._book("09:30")])
        self.assertEqual([r["success"] for r in results], [True, True])

    def test_dos_slots_libres_alrededor_de_uno_ocupado(self):
        self._book("10:00")
        results = self._run_threads([lambda: self._book("09:00"), lambda: self._book("10:30")])
        self.assertEqual([r["success"] for r in results], [True, True])
        rows = self._query(
            "SELECT id FROM appointments WHERE appointment_date = ? AND status = 'confirmed'",
            (self.valid_date,),
        )
        self.assertEqual(len(rows), 3)


class TestConcurrentResourceIsolation(_BaseConcurrencyTest):
    def setUp(self):
        super().setUp()
        self.resource_a = database.create_resource_scoped(1, "Silla A")
        self.resource_b = database.create_resource_scoped(1, "Silla B")

    def test_mismo_slot_distintos_recursos_ambas_exitosas(self):
        results = self._run_threads(
            [
                lambda: self._book("09:00", resource_id=self.resource_a),
                lambda: self._book("09:00", resource_id=self.resource_b),
            ]
        )
        self.assertEqual([r["success"] for r in results], [True, True])

        rows = self._query(
            "SELECT resource_id FROM appointments WHERE appointment_date = ? "
            "AND status = 'confirmed'",
            (self.valid_date,),
        )
        self.assertEqual(
            sorted(r["resource_id"] for r in rows), sorted([self.resource_a, self.resource_b])
        )

    def test_mismo_slot_mismo_recurso_solo_una_exitosa(self):
        results = self._run_threads(
            [
                lambda: self._book("09:00", resource_id=self.resource_a),
                lambda: self._book("09:00", resource_id=self.resource_a),
            ]
        )
        self.assertEqual([r["success"] for r in results].count(True), 1)

    def test_recurso_y_global_conflictivos_solo_una_exitosa(self):
        results = self._run_threads(
            [lambda: self._book("09:00", resource_id=self.resource_a), lambda: self._book("09:00")]
        )
        self.assertEqual([r["success"] for r in results].count(True), 1)

        rows = self._query(
            "SELECT id FROM appointments WHERE appointment_date = ? AND status = 'confirmed'",
            (self.valid_date,),
        )
        self.assertEqual(len(rows), 1)

    def test_recurso_no_conflicto_con_otro_recurso_similar(self):
        first = self._book("09:00", resource_id=self.resource_a)
        self.assertTrue(first["success"])
        second = self._book("09:15", resource_id=self.resource_b)
        self.assertTrue(second["success"])


if __name__ == "__main__":
    unittest.main()
