"""Tests del guardado atómico de horarios semanales.

Cubre `_validate_weekly_schedule_entry` y `save_weekly_schedules_scoped`
de `database/database.py`:

- entradas válidas e inválidas (rango de día, rangos incompletos,
  start >= end, cerrado con horarios, formatos);
- contrato: la validación falla con ValueError (nunca con False silencioso);
- atomicidad: un día inválido o un fallo de BD no persiste ningún día;
- aislamiento por business_id.

Ejecuta contra PostgreSQL mediante `tests._pg_compat.PostgreSQLTestCase`: cada test
recibe una base `turnobot_test_<uuid>` desechable con la semilla estándar (negocio 1).
No hay swap de `DATABASE_PATH` ni `init_database()`: sobre el backend PostgreSQL el
`DATABASE_PATH` solo se lee en la rama SQLite de `get_connection()` y
`init_database()` se limita a detectar que el schema ya está aplicado, así que ambos
eran andamiaje muerto. El aislamiento entre tests es el de la base temporal.

El contexto de aplicación se empuja en `setUp` para que `get_connection()` resuelva el
pool vía `flask.current_app` y no por el respaldo global `_global_pool`.
"""

import unittest
from unittest import mock

import database.database as database
from database.database import get_connection
from tests._pg_compat import PostgreSQLTestCase


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


class TestSaveWeeklySchedulesScoped(unittest.TestCase, PostgreSQLTestCase):
    def setUp(self):
        # `self.app` lo aporta la fixture `app`: app y pool propios de este test,
        # apuntando a la base temporal creada por `pg_test_database`. El contexto se
        # empuja aquí (no en la fixture) porque el servicio y los helpers de este
        # archivo llaman `get_connection()` directamente.
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.addCleanup(self._app_context.pop)

    def _schedules(self, business_id=1):
        c = get_connection()
        try:
            rows = c.execute(
                "SELECT day_of_week, is_open, morning_start, morning_end,"
                " afternoon_start, afternoon_end FROM weekly_schedules"
                " WHERE business_id = %s ORDER BY day_of_week",
                (business_id,),
            ).fetchall()
            # PostgreSQL devuelve tipos nativos (bool, time): se normalizan a la
            # representación de SQLite (0/1, "HH:MM") que es la que esperan las
            # aserciones de este archivo.
            out = []
            for r in rows:
                v = list(r.values())
                v[1] = int(v[1])
                for i in range(2, 6):
                    if v[i] is not None:
                        v[i] = v[i].strftime("%H:%M")
                out.append(tuple(v))
            return out
        finally:
            c.close()

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
        c = get_connection()
        try:
            c.execute(
                "INSERT INTO businesses (id, name, slug) VALUES (%s, %s, %s)", (2, "otro", "otro")
            )
            c.commit()
            for day in range(7):
                c.execute(
                    "INSERT INTO weekly_schedules (business_id, day_of_week, is_open) VALUES (%s, %s, %s)",
                    (2, day, 0),
                )
            c.commit()
        finally:
            c.close()

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
