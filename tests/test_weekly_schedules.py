"""Tests del guardado atómico de horarios semanales.

Cubre `_validate_weekly_schedule_entry` y `save_weekly_schedules_scoped`
de `database/database.py`:

- entradas válidas e inválidas (rango de día, rangos incompletos,
  start >= end, cerrado con horarios, formatos);
- contrato: la validación falla con ValueError (nunca con False silencioso);
- atomicidad: un día inválido o un fallo de BD no persiste ningún día;
- aislamiento por business_id.

Usan SQLite temporal (patrón de tests/test_backup_database.py): ninguna
operación contra PostgreSQL.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

import database.database as database

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests SQLite puros: no se necesita PostgreSQL, por lo que no se aplica
    el skip funcional del conftest (omite la suite cuando TURNOBOT_PG_URL no
    está definida)."""
    yield


def _entry(day, is_open=True, ms="09:00", me="13:00", as_="15:00", ae="20:00"):
    return {
        "day_of_week": day,
        "is_open": is_open,
        "morning_start": ms,
        "morning_end": me,
        "afternoon_start": as_,
        "afternoon_end": ae,
    }


def _closed(day):
    return {
        "day_of_week": day,
        "is_open": False,
        "morning_start": None,
        "morning_end": None,
        "afternoon_start": None,
        "afternoon_end": None,
    }


def _week():
    return [_entry(d) if d < 5 else _closed(d) for d in range(7)]


class TestValidateWeeklyScheduleEntry(unittest.TestCase):
    def test_entrada_valida_abierta_y_cerrada(self):
        database._validate_weekly_schedule_entry(_entry(0))
        database._validate_weekly_schedule_entry(_closed(6))
        database._validate_weekly_schedule_entry(_entry(3, ms="09:00", me="09:01"))

    def test_day_of_week_fuera_de_rango(self):
        for bad in (-1, 7, 99, None, "x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    database._validate_weekly_schedule_entry(_entry(bad))

    def test_abierto_sin_ningun_horario(self):
        with self.assertRaises(ValueError):
            database._validate_weekly_schedule_entry(_entry(1, ms=None, me=None, as_=None, ae=None))

    def test_rango_incompleto(self):
        with self.assertRaises(ValueError):
            database._validate_weekly_schedule_entry(_entry(1, ms="09:00", me=None))
        with self.assertRaises(ValueError):
            database._validate_weekly_schedule_entry(_entry(1, ms=None, me="13:00"))

    def test_start_mayor_o_igual_que_end(self):
        with self.assertRaises(ValueError):
            database._validate_weekly_schedule_entry(_entry(1, ms="13:00", me="09:00"))
        with self.assertRaises(ValueError):
            database._validate_weekly_schedule_entry(_entry(1, ms="09:00", me="09:00"))

    def test_cerrado_con_horarios_informados(self):
        bad = _closed(6)
        bad["morning_start"] = "09:00"
        with self.assertRaises(ValueError):
            database._validate_weekly_schedule_entry(bad)

    def test_formato_de_hora_invalido(self):
        for bad_time in ("9", "09-00", "25:00", "09:60", "ab:cd", "09:00:00"):
            with self.subTest(bad_time=bad_time):
                with self.assertRaises(ValueError):
                    database._validate_weekly_schedule_entry(_entry(1, ms=bad_time, me="13:00"))


class TestSaveWeeklySchedulesScoped(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.root_dir / "appointments.db"

        self._original_db_backend = os.environ.get("DB_BACKEND")
        self._original_db_url = os.environ.get("DATABASE_URL")
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)

        database.init_database()
        self._open_connections = []

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        if self._original_db_backend is not None:
            os.environ["DB_BACKEND"] = self._original_db_backend
        else:
            os.environ.pop("DB_BACKEND", None)
        if self._original_db_url is not None:
            os.environ["DATABASE_URL"] = self._original_db_url
        else:
            os.environ.pop("DATABASE_URL", None)
        for conn in self._open_connections:
            conn.close()
        self.temp_dir.cleanup()

    def _conn(self):
        conn = database.get_connection()
        self._open_connections.append(conn)
        return conn

    def _schedules(self, business_id=1):
        rows = (
            self._conn()
            .execute(
                "SELECT day_of_week, is_open, morning_start, morning_end,"
                " afternoon_start, afternoon_end FROM weekly_schedules"
                " WHERE business_id = ? ORDER BY day_of_week",
                (business_id,),
            )
            .fetchall()
        )
        return [tuple(r) for r in rows]

    def test_guarda_semana_valida_y_devuelve_true(self):
        self.assertTrue(database.save_weekly_schedules_scoped(1, _week()))
        rows = self._schedules(1)
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0][1:], (1, "09:00", "13:00", "15:00", "20:00"))
        self.assertEqual(rows[6][1:], (0, None, None, None, None))

    def test_validacion_falla_con_valueerror_y_no_persiste_nada(self):
        before = self._schedules(1)
        week = _week()
        week[4] = _entry(4, ms="18:00", me="09:00")  # start > end
        with self.assertRaises(ValueError):
            database.save_weekly_schedules_scoped(1, week)
        self.assertEqual(self._schedules(1), before)

    def test_dia_duplicado_rechaza_lote_completo(self):
        before = self._schedules(1)
        week = _week() + [_entry(0)]
        with self.assertRaises(ValueError):
            database.save_weekly_schedules_scoped(1, week)
        self.assertEqual(self._schedules(1), before)

    def test_entrada_no_dict_y_schedules_none_fallan(self):
        with self.assertRaises(ValueError):
            database.save_weekly_schedules_scoped(1, ["no-un-dict"])
        with self.assertRaises(ValueError):
            database.save_weekly_schedules_scoped(1, None)

    def test_fallo_de_bd_revierte_todo(self):
        real_connect = database.get_connection

        calls = {"updates": 0}
        real_conn = real_connect()
        self._open_connections.append(real_conn)
        real_execute = real_conn.execute

        def flaky_execute(sql, params=None):
            if sql.strip().upper().startswith("UPDATE WEEKLY_SCHEDULES"):
                calls["updates"] += 1
                if calls["updates"] == 2:
                    raise RuntimeError("fallo simulado de BD")
            return real_execute(sql, params) if params is not None else real_execute(sql)

        before = self._schedules(1)
        with mock.patch.object(database, "get_connection") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_conn.execute.side_effect = flaky_execute
            mock_connect.return_value = mock_conn
            with self.assertRaises(RuntimeError):
                database.save_weekly_schedules_scoped(1, _week())
            mock_conn.execute.assert_any_call("ROLLBACK")

        # Nada del lote quedó aplicado a pesar de que el primer UPDATE corrió.
        self.assertEqual(calls["updates"], 2)
        self.assertEqual(self._schedules(1), before)

    def test_aislamiento_por_business_id(self):
        conn = self._conn()
        conn.execute("INSERT INTO businesses (id, name, slug) VALUES (2, 'otro', 'otro')")
        for day in range(7):
            conn.execute(
                "INSERT INTO weekly_schedules (business_id, day_of_week, is_open) VALUES (2, ?, 0)",
                (day,),
            )
        conn.commit()

        week_b2 = [_closed(d) for d in range(7)]
        week_b2[0] = _entry(0, ms="08:00", me="12:00", as_=None, ae=None)
        self.assertTrue(database.save_weekly_schedules_scoped(2, week_b2))

        rows_b2 = self._schedules(2)
        self.assertEqual(rows_b2[0][1:], (1, "08:00", "12:00", None, None))
        # El negocio 1 conserva sus valores semilla intactos.
        rows_b1 = self._schedules(1)
        self.assertEqual(len(rows_b1), 7)


if __name__ == "__main__":
    unittest.main()
