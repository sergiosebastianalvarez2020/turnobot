"""Tests funcionales de intervals de turnos: duración, solapamiento, cierre.

Ejecutan contra PostgreSQL usando la fixture de base temporal aislada.
El backend SQLite se mantiene únicamente en test_appointment_intervals_migration.py
que valida la migración histórica de esquema SQLite → PostgreSQL.
"""

import threading
import unittest
from datetime import datetime, timedelta

import database.database as database
from services import appointments
from tests._pg_compat import PostgreSQLTestCase


def _next_open_day():
    """Próximo día que no es domingo."""
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


def _day_of_week(date_iso):
    """Extrae day_of_week (0=lunes..6=domingo) desde una fecha ISO."""
    return datetime.fromisoformat(date_iso).weekday()


class BaseIntervalTest(unittest.TestCase, PostgreSQLTestCase):
    """Business 1 (El Corte) en base temporal PostgreSQL.
    Se configura grilla fina (slot_duration=15) para poder expresar horarios como 09:15."""

    DATE = None

    def setUp(self):
        with self.app.app_context():
            self.valid_date = _next_open_day()
            BaseIntervalTest.DATE = self.valid_date

            weekday = datetime.now().weekday()
            while weekday == 6:
                weekday = (weekday + 1) % 7

            self._execute(
                "UPDATE business_settings SET slot_duration = 15, break_between_slots = 0 "
                "WHERE business_id = 1"
            )
            self._execute(
                "UPDATE weekly_schedules SET is_open = TRUE, morning_start = '09:00', "
                "morning_end = '12:00', afternoon_start = NULL, afternoon_end = NULL "
                "WHERE business_id = 1 AND day_of_week = %s",
                (weekday,),
            )
            self._execute("UPDATE services SET business_id = 1 WHERE id IN (1, 2, 3)")
            self.services = {
                "S15": self._insert_service("S15", 15),
                "S20": self._insert_service("S20", 20),
                "S30": self._insert_service("S30", 30),
                "S50": self._insert_service("S50", 50),
                "S60": self._insert_service("S60", 60),
            }

    def _execute(self, sql, params=None):
        with self.app.app_context():
            connection = database.get_connection()
            try:
                connection.execute(sql, params or ())
                connection.commit()
            finally:
                connection.close()

    def _query(self, sql, params=None):
        with self.app.app_context():
            connection = database.get_connection()
            try:
                return connection.execute(sql, params or ()).fetchall()
            finally:
                connection.close()

    def _insert_service(self, name, duration):
        with self.app.app_context():
            connection = database.get_connection()
            try:
                cursor = connection.execute(
                    "INSERT INTO services (business_id, name, price, duration, active) "
                    "VALUES (1, %s, 1000, %s, TRUE)",
                    (name, duration),
                )
                connection.commit()
                return cursor.lastrowid
            finally:
                connection.close()

    def _book(self, service_name, time, business_id=1, name="Cliente", phone="123456789"):
        return appointments.create_appointment(
            name, phone, service_name, self.valid_date, time, business_id
        )


class TestDurationAndEnd(BaseIntervalTest):
    def test_duracion_20(self):
        result = self._book("S20", "09:00")
        assert result["success"] is True
        row = self._query(
            "SELECT duration, appointment_end FROM appointments WHERE id = %s",
            (result["appointment_id"],),
        )[0]
        assert row["duration"] == 20
        assert _fmt_time(row["appointment_end"]) == "09:20"

    def test_duracion_30(self):
        result = self._book("S30", "09:00")
        assert result["success"] is True
        row = self._query(
            "SELECT duration, appointment_end FROM appointments WHERE id = %s",
            (result["appointment_id"],),
        )[0]
        assert row["duration"] == 30
        assert _fmt_time(row["appointment_end"]) == "09:30"

    def test_duracion_50(self):
        result = self._book("S50", "09:00")
        assert result["success"] is True
        row = self._query(
            "SELECT duration, appointment_end FROM appointments WHERE id = %s",
            (result["appointment_id"],),
        )[0]
        assert row["duration"] == 50
        assert _fmt_time(row["appointment_end"]) == "09:50"

    def test_duracion_60(self):
        result = self._book("S60", "09:00")
        assert result["success"] is True
        row = self._query(
            "SELECT duration, appointment_end FROM appointments WHERE id = %s",
            (result["appointment_id"],),
        )[0]
        assert row["duration"] == 60
        assert _fmt_time(row["appointment_end"]) == "10:00"


class TestOverlap(BaseIntervalTest):
    def setUp(self):
        super().setUp()
        self._book("S60", "09:00")

    def test_09_15_a_09_30_rechazar(self):
        result = self._book("S15" if "S15" in self.services else "S20", "09:15")
        assert result["success"] is False
        assert result["reason"] == "occupied"

    def test_09_30_a_10_00_rechazar(self):
        result = self._book("S30", "09:30")
        assert result["success"] is False
        assert result["reason"] == "occupied"

    def test_10_00_a_10_30_permitir(self):
        result = self._book("S30", "10:00")
        assert result["success"] is True


class TestCrossBusiness(BaseIntervalTest):
    def test_mismo_horario_distinto_negocio_permitido(self):
        self._execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )
        for day in range(7):
            self._execute(
                "INSERT INTO weekly_schedules "
                "(business_id, day_of_week, is_open, morning_start, morning_end, "
                "afternoon_start, afternoon_end) "
                "VALUES (2, %s, TRUE, '09:00', '12:00', NULL, NULL)",
                (day,),
            )
        self._insert_business_b_service("S60 B", 60)

        a = self._book("S60", "09:00", business_id=1)
        b = self._book("S60 B", "09:00", business_id=2)
        assert a["success"] is True
        assert b["success"] is True

    def _insert_business_b_service(self, name, duration):
        with self.app.app_context():
            connection = database.get_connection()
            try:
                cursor = connection.execute(
                    "INSERT INTO services (business_id, name, price, duration, active) "
                    "VALUES (2, %s, 1000, %s, TRUE)",
                    (name, duration),
                )
                connection.commit()
                return cursor.lastrowid
            finally:
                connection.close()


class TestHistoricalDuration(BaseIntervalTest):
    def test_cambio_de_duracion_conserva_historia(self):
        result = self._book("S30", "09:00")
        assert result["success"] is True
        appointment_id = result["appointment_id"]

        self._execute("UPDATE services SET duration = 60 WHERE id = %s", (self.services["S30"],))

        row = self._query(
            "SELECT duration, appointment_end FROM appointments WHERE id = %s", (appointment_id,)
        )[0]
        assert row["duration"] == 30
        assert _fmt_time(row["appointment_end"]) == "09:30"


class TestClosingTime(BaseIntervalTest):
    def test_termina_exacto_al_cierre_permitido(self):
        self._execute(
            "UPDATE weekly_schedules SET is_open = TRUE, morning_start = '09:00', "
            "morning_end = '10:00', afternoon_start = NULL, afternoon_end = NULL "
            "WHERE business_id = 1 AND day_of_week = %s",
            (_day_of_week(self.valid_date),),
        )
        result = self._book("S60", "09:00")
        assert result["success"] is True
        row = self._query(
            "SELECT appointment_end FROM appointments WHERE id = %s", (result["appointment_id"],)
        )[0]
        assert _fmt_time(row["appointment_end"]) == "10:00"

    def test_excede_el_cierre_rechazado(self):
        self._execute(
            "UPDATE weekly_schedules SET is_open = TRUE, morning_start = '09:00', "
            "morning_end = '10:00', afternoon_start = NULL, afternoon_end = NULL "
            "WHERE business_id = 1 AND day_of_week = %s",
            (_day_of_week(self.valid_date),),
        )
        result = self._book("S60", "09:30")
        assert result["success"] is False
        assert result["reason"] == "invalid_time"


class TestConcurrency(BaseIntervalTest):
    def test_dos_reservas_solapadas_simultaneas_solo_una_confirmada(self):
        barrier = threading.Barrier(2)
        results = []

        def book():
            barrier.wait()
            results.append(self._book("S30", "09:00"))

        threads = [threading.Thread(target=book) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r["success"] for r in results]
        assert successes.count(True) == 1
        assert len(results) == 2

    def test_dos_reprogramaciones_solapadas_simultaneas_solo_una_exitosa(self):
        barrier = threading.Barrier(2)
        results = []

        result_a = self._book("S30", "09:00")
        appointment_id_a = result_a["appointment_id"]
        result_b = self._book("S30", "10:00")
        appointment_id_b = result_b["appointment_id"]

        def reschedule(appointment_id, management_token):
            barrier.wait()
            result = appointments.reschedule_appointment(
                appointment_id=appointment_id,
                new_date=self.valid_date,
                new_time="10:30",
                phone="123456789",
                business_id=1,
                management_token=management_token,
                customer_name="Cliente",
            )
            results.append(result)

        threads = [
            threading.Thread(
                target=reschedule, args=(appointment_id_a, result_a["management_token"])
            ),
            threading.Thread(
                target=reschedule, args=(appointment_id_b, result_b["management_token"])
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r["success"] for r in results]
        assert successes.count(True) == 1
        assert len(results) == 2

        rows = self._query(
            "SELECT id, appointment_time FROM appointments "
            "WHERE appointment_date = %s AND status = 'confirmed'",
            (self.valid_date,),
        )
        at_1030 = [r for r in rows if _fmt_time(r["appointment_time"]) == "10:30"]
        assert len(at_1030) == 1


class TestInvalidDuration(BaseIntervalTest):
    def test_duracion_cero_rechazada(self):
        self._execute("UPDATE services SET duration = 0 WHERE id = %s", (self.services["S30"],))
        result = self._book("S30", "09:00")
        assert result["success"] is False
        assert result["reason"] == "invalid_duration"

    def test_duracion_negativa_rechazada(self):
        self._execute("UPDATE services SET duration = -1 WHERE id = %s", (self.services["S30"],))
        result = self._book("S30", "09:00")
        assert result["success"] is False
        assert result["reason"] == "invalid_duration"

    def test_duracion_null_bloqueada_por_esquema(self):
        sql = (
            "INSERT INTO services (business_id, name, price, duration, active) "
            "VALUES (1, 'S NULL', 1000, NULL, TRUE)"
        )
        with self.app.app_context():
            connection = database.get_connection()
            try:
                connection.execute(sql)
                exception = None
            except Exception as exc:
                exception = exc
                connection.execute("ROLLBACK")
            finally:
                connection.close()
            assert exception is not None


def _fmt_time(val):
    """Normaliza TIME para comparación: str o datetime.time -> 'HH:MM'."""
    if isinstance(val, str):
        return val
    return val.strftime("%H:%M")
