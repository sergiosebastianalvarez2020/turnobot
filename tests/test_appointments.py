"""Tests funcionales de appointments: reservas, cancelación, reprogramación, locks, idempotencia.

Ejecutan contra PostgreSQL usando la fixture de base temporal aislada.
El backend SQLite se mantiene únicamente en tests de migración histórica
(ver test_appointment_idempotency_migration.py) y herramientas de backup
que realmente dependen del formato SQLite.
"""

import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import database.database as database
from services import appointments
from tests._pg_compat import PostgreSQLTestCase

FROZEN_NOW = datetime(
    2027, 1, 24, 10, 0
)  # domingo fijo: next_open_day() cae en lunes con turno de tarde

ZONA_HORARIO = ZoneInfo("America/Argentina/Buenos_Aires")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


def _next_open_day():
    """Próximo día que no es domingo."""
    date = datetime.now(ZONA_HORARIO).date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class TestReservaDisponible(PostgreSQLTestCase):
    def setup_method(self):
        self.valid_date = _next_open_day()

    def test_reserva_en_horario_disponible(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        assert result["success"] is True
        assert result["appointment_id"] is not None


class TestReservaOcupada(PostgreSQLTestCase):
    def setup_method(self):
        self.valid_date = _next_open_day()

    def test_reserva_en_horario_ocupado(self):
        appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        result = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "09:00", 1
        )
        assert result["success"] is False
        assert result["reason"] == "occupied"


class TestDomingoCerrado(PostgreSQLTestCase):
    def test_reserva_en_domingo(self):
        sunday = datetime.now(ZONA_HORARIO).date()
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", sunday.isoformat(), "09:00", 1
        )
        assert result["success"] is False
        assert result["reason"] == "closed_day"


class TestFechaInvalida(PostgreSQLTestCase):
    def test_reserva_con_fecha_mal_formateada(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", "2025/08/18", "09:00", 1
        )
        assert result["success"] is False
        assert result["reason"] == "invalid_date"

    def test_reserva_con_fecha_pasada(self):
        yesterday = (datetime.now(ZONA_HORARIO).date() - timedelta(days=1)).isoformat()
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", yesterday, "09:00", 1
        )
        assert result["success"] is False
        assert result["reason"] == "past_date"

    def test_reserva_con_hora_invalida(self):
        future = datetime.now(ZONA_HORARIO).date() + timedelta(days=2)
        while future.weekday() == 6:
            future += timedelta(days=1)
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", future.isoformat(), "25:99", 1
        )
        assert result["success"] is False
        assert result["reason"] == "invalid_time"


class TestCancelacionTelefonoCorrecto(PostgreSQLTestCase):
    def setup_method(self):
        self.valid_date = _next_open_day()

    def test_cancelar_con_telefono_correcto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        appointment_id = result["appointment_id"]
        assert appointments.cancel_appointment(
            appointment_id, "3838439222", 1, result["management_token"], customer_name="Ana Pérez"
        )


class TestCancelacionTelefonoIncorrecto(PostgreSQLTestCase):
    def setup_method(self):
        self.valid_date = _next_open_day()

    def test_cancelar_con_telefono_incorrecto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        appointment_id = result["appointment_id"]
        assert not appointments.cancel_appointment(
            appointment_id, "3838439222", 1, "token_incorrecto", customer_name="Ana Pérez"
        )


class TestReprogramacionHorarioOcupado(PostgreSQLTestCase):
    def setup_method(self):
        self._datetime_patch = mock.patch(f"{__name__}.datetime", _FrozenDatetime)
        self._datetime_patch.start()
        self._service_datetime_patch = mock.patch("services.appointments.datetime", _FrozenDatetime)
        self._service_datetime_patch.start()
        self.valid_date = _next_open_day()

    def teardown_method(self):
        self._datetime_patch.stop()
        self._service_datetime_patch.stop()

    def test_reprogramar_a_horario_ocupado(self):
        appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        result2 = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "15:00", 1
        )
        appointment_id = result2["appointment_id"]
        result = appointments.reschedule_appointment(
            appointment_id,
            self.valid_date,
            "09:00",
            "3838439333",
            1,
            result2["management_token"],
            customer_name="Juan López",
        )
        assert not result["success"]
        assert result["reason"] == "occupied"


class TestDobleReservaSimultanea(PostgreSQLTestCase):
    def setup_method(self):
        self.valid_date = _next_open_day()

    def test_doble_reserva_mismo_horario(self):
        result1 = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        result2 = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "09:00", 1
        )
        assert result1["success"]
        assert not result2["success"]
        assert result2["reason"] == "occupied"


class _BaseConcurrencyAppointments(unittest.TestCase, PostgreSQLTestCase):
    """Preparación compartida de calendario para concurrencia PostgreSQL real."""

    def setUp(self):
        self.valid_date = _next_open_day()
        self.business_id = 1
        with self.app.app_context():
            connection = database.get_connection()
            try:
                connection.execute(
                    "UPDATE business_settings SET slot_duration = %s, break_between_slots = %s "
                    "WHERE business_id = %s",
                    (15, 0, self.business_id),
                )
                connection.execute(
                    "UPDATE weekly_schedules SET is_open = TRUE, morning_start = %s, "
                    "morning_end = %s, afternoon_start = NULL, afternoon_end = NULL "
                    "WHERE business_id = %s AND day_of_week = %s",
                    (
                        "09:00",
                        "12:00",
                        self.business_id,
                        datetime.fromisoformat(self.valid_date).weekday(),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

    def _book(self, time_, resource_id=None):
        with self.app.app_context():
            return appointments.create_appointment(
                "Cliente",
                "123456789",
                "Corte",
                self.valid_date,
                time_,
                self.business_id,
                resource_id=resource_id,
            )

    def _run_concurrently(self, workers):
        """Cada worker opera simultáneamente y obtiene su conexión del pool PG."""
        barrier = threading.Barrier(len(workers))
        results = [None] * len(workers)

        def run(index, worker):
            try:
                with self.app.app_context():
                    barrier.wait(timeout=15)
                    results[index] = worker()
            except Exception as exc:  # noqa: BLE001 - propaga el fallo en la assertion
                results[index] = exc

        threads = [
            threading.Thread(target=run, args=(index, worker))
            for index, worker in enumerate(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert all(not thread.is_alive() for thread in threads), "un worker quedó bloqueado"
        assert all(not isinstance(result, Exception) for result in results), str(results)
        return results


class TestConcurrentBookings(_BaseConcurrencyAppointments):
    def test_slots_distintos_simultaneos_ambas_exitosas(self):
        results = self._run_concurrently([lambda: self._book("09:00"), lambda: self._book("09:30")])
        assert [result["success"] for result in results] == [True, True]

    def test_dos_slots_libres_alrededor_de_uno_ocupado(self):
        assert self._book("10:00")["success"]
        results = self._run_concurrently([lambda: self._book("09:00"), lambda: self._book("10:30")])
        assert [result["success"] for result in results] == [True, True]
        with self.app.app_context():
            connection = database.get_connection()
            try:
                rows = connection.execute(
                    "SELECT id FROM appointments "
                    "WHERE appointment_date = %s AND status = 'confirmed'",
                    (datetime.fromisoformat(self.valid_date).date(),),
                ).fetchall()
            finally:
                connection.close()
        assert len(rows) == 3


class TestConcurrentResourceIsolation(_BaseConcurrencyAppointments):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            self.resource_a = database.create_resource_scoped(self.business_id, "Silla A")
            self.resource_b = database.create_resource_scoped(self.business_id, "Silla B")

    def test_mismo_slot_distintos_recursos_ambas_exitosas(self):
        results = self._run_concurrently(
            [
                lambda: self._book("09:00", resource_id=self.resource_a),
                lambda: self._book("09:00", resource_id=self.resource_b),
            ]
        )
        assert [result["success"] for result in results] == [True, True]
        with self.app.app_context():
            connection = database.get_connection()
            try:
                rows = connection.execute(
                    "SELECT resource_id FROM appointments "
                    "WHERE appointment_date = %s AND status = 'confirmed'",
                    (datetime.fromisoformat(self.valid_date).date(),),
                ).fetchall()
            finally:
                connection.close()
        assert {row["resource_id"] for row in rows} == {self.resource_a, self.resource_b}

    def test_mismo_slot_mismo_recurso_solo_una_exitosa(self):
        results = self._run_concurrently(
            [
                lambda: self._book("09:00", resource_id=self.resource_a),
                lambda: self._book("09:00", resource_id=self.resource_a),
            ]
        )
        assert [result["success"] for result in results].count(True) == 1

    def test_recurso_y_global_conflictivos_solo_una_exitosa(self):
        results = self._run_concurrently(
            [lambda: self._book("09:00", resource_id=self.resource_a), lambda: self._book("09:00")]
        )
        assert [result["success"] for result in results].count(True) == 1
        with self.app.app_context():
            connection = database.get_connection()
            try:
                rows = connection.execute(
                    "SELECT id FROM appointments "
                    "WHERE appointment_date = %s AND status = 'confirmed'",
                    (datetime.fromisoformat(self.valid_date).date(),),
                ).fetchall()
            finally:
                connection.close()
        assert len(rows) == 1

    def test_recurso_no_conflicto_con_otro_recurso_similar(self):
        first = self._book("09:00", resource_id=self.resource_a)
        assert first["success"]
        second = self._book("09:15", resource_id=self.resource_b)
        assert second["success"]


class TestCancelacionTomaElLockDeEscritura(PostgreSQLTestCase):
    """`cancel_appointment` debe serializarse con creación/reprogramación."""

    def setup_method(self):
        self.valid_date = _next_open_day()
        self.original_lock = appointments.acquire_business_write_lock
        self.lock_calls = []
        appointments.acquire_business_write_lock = self._spy

    def teardown_method(self):
        appointments.acquire_business_write_lock = self.original_lock

    def _spy(self, connection, business_id, lock_key=0):
        self.lock_calls.append((business_id, lock_key))

    def test_cancelar_toma_el_lock_del_dia_del_turno(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        assert result["success"]
        self.lock_calls.clear()

        assert appointments.cancel_appointment(
            result["appointment_id"],
            "3838439222",
            1,
            result["management_token"],
            customer_name="Ana Pérez",
        )

        esperado = (1, appointments._appointment_day_ordinal(self.valid_date))
        assert self.lock_calls == [esperado]

    def test_cancelar_no_toma_el_lock_si_el_turno_no_existe(self):
        resultado = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.lock_calls.clear()

        assert not appointments.cancel_appointment(
            resultado["appointment_id"] + 999, "3838439222", 1, resultado["management_token"]
        )
        assert self.lock_calls == []

    def test_cancelar_toma_el_lock_una_sola_vez(self):
        """El turno se relee para la clave del lock y se revalida después."""
        resultado = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.lock_calls.clear()

        appointments.cancel_appointment(
            resultado["appointment_id"], "3838439222", 1, resultado["management_token"]
        )

        assert len(self.lock_calls) == 1


class TestIdempotenciaDeReserva(PostgreSQLTestCase):
    """`idempotency_key` hace que un reintento devuelva el turno original.

    Mismo patrón que `loyalty.redeem`: clave del cliente + índice único
    (business_id, idempotency_key) + lookup dentro del advisory lock.
    """

    def setup_method(self):
        self.valid_date = _next_open_day()

    def _count(self):
        c = database.get_connection()
        try:
            return c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        finally:
            c.close()

    def _reservar(self, idempotency_key=None, hora="09:00", nombre="Ana Pérez"):
        return appointments.create_appointment(
            nombre, "3838439222", "Corte", self.valid_date, hora, 1, idempotency_key=idempotency_key
        )

    def test_reintento_devuelve_el_mismo_turno(self):
        primero = self._reservar("clave-abc")
        assert primero["success"]
        assert primero["reason"] == "created"

        segundo = self._reservar("clave-abc")
        assert segundo["success"], "un reintento no debe fallar por 'occupied'"
        assert segundo["reason"] == "already_created"
        assert segundo["idempotent_replay"]
        assert segundo["appointment_id"] == primero["appointment_id"]
        assert self._count() == 1, "el reintento no debe crear un segundo turno"

    def test_el_replay_no_devuelve_el_management_token(self):
        primero = self._reservar("clave-abc")
        segundo = self._reservar("clave-abc")
        assert primero["management_token"]
        assert segundo["management_token"] is None, "el token no es reconstruible"

    def test_el_replay_conserva_los_datos_del_turno_original(self):
        primero = self._reservar("clave-abc")
        # Misma clave con payload DISTINTO: la clave identifica la operación.
        segundo = self._reservar("clave-abc", hora="11:00", nombre="Bruno Gómez")
        assert segundo["success"]
        assert segundo["appointment_id"] == primero["appointment_id"]
        # El replay conserva los datos del turno original, no del replay.
        assert segundo["appointment_time"] == primero["appointment_time"]
        assert segundo["customer_name"] == primero["customer_name"]
        assert self._count() == 1

    def test_claves_distintas_crean_turnos_distintos(self):
        assert self._reservar("clave-1", hora="09:00")["success"]
        assert self._reservar("clave-2", hora="11:00")["success"]
        assert self._count() == 2

    def test_sin_clave_el_comportamiento_es_el_historico(self):
        primero = self._reservar()
        assert primero["success"]
        assert "idempotent_replay" not in primero
        assert primero["reason"] == "created"

        segundo = self._reservar()
        assert not segundo["success"]
        assert segundo["reason"] == "occupied"

    def test_turnos_sin_clave_no_colisionan_en_el_indice(self):
        """NULL no participa del índice parcial: dos altas sin clave conviven."""
        assert self._reservar()["success"]
        assert self._reservar(hora="11:00")["success"]
        assert self._count() == 2

    def test_la_clave_no_atreves_negocios(self):
        """El UNIQUE es (business_id, idempotency_key), no solo la clave."""
        primero = self._reservar("clave-compartida")
        assert primero["success"]

        # Verificar que el índice único incluye business_id (no solo idempotency_key).
        c = database.get_connection()
        try:
            idx = c.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'appointments'"
            ).fetchall()
        finally:
            c.close()

        found = False
        for row in idx:
            if "idempotency_key" in row["indexdef"]:
                assert "business_id" in row["indexdef"], (
                    "El índice de idempotencia debe incluir business_id"
                )
                found = True
        assert found, "No se encontró índice unique sobre (business_id, idempotency_key)"

    def test_relee_al_ganador_si_el_indice_unico_dispara(self):
        """La rama `IntegrityError` de la idempotencia relee al ganador."""
        ganador = self._reservar("clave-carrera")
        assert ganador["success"]

        real_get_connection = database.get_connection
        objetivo = appointments.get_connection

        class _ReplayCiego:
            """Omite el primer SELECT de replay; fuerza la violación del índice."""

            def __init__(self, connection):
                self._connection = connection
                self._tapado = False

            def execute(self, sql, params=()):
                if (
                    not self._tapado
                    and "idempotency_key = ?" in sql
                    and sql.lstrip().startswith("SELECT id, customer_name")
                ):
                    self._tapado = True
                    return self._connection.execute(
                        "SELECT id, customer_name, customer_email, service, appointment_date, "
                        "appointment_time, appointment_end, duration, resource_id "
                        "FROM appointments WHERE id = -1"
                    )
                return self._connection.execute(sql, params)

            def __getattr__(self, nombre):
                return getattr(self._connection, nombre)

        def conexion_cegada():
            return _ReplayCiego(real_get_connection())

        appointments.get_connection = conexion_cegada
        try:
            replay = appointments.create_appointment(
                "Bruno Gómez",
                "3838439222",
                "Corte",
                self.valid_date,
                "11:00",
                1,
                idempotency_key="clave-carrera",
            )
        finally:
            appointments.get_connection = objetivo

        assert replay["success"], "la UniqueViolation de idempotencia no debe ser 'occupied'"
        assert replay["reason"] == "already_created"
        assert replay["appointment_id"] == ganador["appointment_id"]
        assert self._count() == 1


class _CursorRowcountCero:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return 0

    def __getattr__(self, nombre):
        return getattr(self._cursor, nombre)


class _ConexionRowcountCero:
    """Proxy que fuerza rowcount=0 en el UPDATE como si la fila ya no existiera."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        cursor = self._connection.execute(sql, *args)
        if sql.lstrip().upper().startswith("UPDATE"):
            return _CursorRowcountCero(cursor)
        return cursor

    def __getattr__(self, nombre):
        return getattr(self._connection, nombre)


class _BaseLockDeReschedule(PostgreSQLTestCase):
    def setup_method(self):
        self.valid_date = _next_open_day()
        self.otro_dia = self._dia_laborable(
            datetime.strptime(self.valid_date, "%Y-%m-%d").date() + timedelta(days=1)
        )
        self.original_lock = appointments.acquire_business_write_lock
        self.original_get_connection = appointments.get_connection
        self.lock_calls = []
        appointments.acquire_business_write_lock = self._spy

    def teardown_method(self):
        appointments.acquire_business_write_lock = self.original_lock
        appointments.get_connection = self.original_get_connection

    def _spy(self, connection, business_id, lock_key=0):
        self.lock_calls.append((business_id, lock_key))

    @staticmethod
    def _dia_laborable(date):
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def _turno(self, fecha=None, hora="09:00"):
        return appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", fecha or self.valid_date, hora, 1
        )

    def _fila(self, appointment_id):
        c = database.get_connection()
        try:
            return c.execute(
                "SELECT appointment_date, appointment_time, status FROM appointments WHERE id = ?",
                (appointment_id,),
            ).fetchone()
        finally:
            c.close()

    def _locks_esperados(self, origen_iso, destino_iso):
        origen = appointments._appointment_day_ordinal(origen_iso)
        destino = appointments._appointment_day_ordinal(destino_iso)
        return [(1, min(origen, destino)), (1, max(origen, destino))]


class TestRescheduleTomaLosLocksDeOrigenYDestino(_BaseLockDeReschedule):
    """`reschedule_appointment` libera un día y ocupa otro: necesita AMBOS locks.

    La serialización real con PostgreSQL se verifica en tests_pg/test_advisory_lock_live.py.
    Aquí se comprueba la CONTRATACIÓN: se piden locks de origen y destino en orden ascendente.
    """

    def test_toma_locks_de_origen_y_destino_en_orden_ascendente(self):
        resultado = self._turno()
        assert resultado["success"]
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment(
            resultado["appointment_id"],
            self.otro_dia,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )

        assert reprogramado["success"], reprogramado
        assert self.lock_calls == self._locks_esperados(self.valid_date, self.otro_dia)

    def test_deduplica_el_lock_si_el_dia_no_cambia(self):
        resultado = self._turno(hora="09:00")
        assert resultado["success"]
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment(
            resultado["appointment_id"],
            self.valid_date,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )

        assert reprogramado["success"], reprogramado
        assert self.lock_calls == [(1, appointments._appointment_day_ordinal(self.valid_date))], (
            "mismo día de origen y destino: un solo lock, no dos"
        )

    def test_no_toma_locks_si_el_turno_no_existe(self):
        resultado = self._turno()
        self.lock_calls.clear()

        fallido = appointments.reschedule_appointment(
            resultado["appointment_id"] + 999,
            self.otro_dia,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )

        assert not fallido["success"]
        assert fallido["reason"] == "not_found"
        assert self.lock_calls == []

    def test_admin_toma_locks_de_origen_y_destino_en_orden_ascendente(self):
        resultado = self._turno()
        assert resultado["success"]
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.otro_dia, "11:00", business_id=1
        )

        assert reprogramado["success"], reprogramado
        assert self.lock_calls == self._locks_esperados(self.valid_date, self.otro_dia)

    def test_admin_deduplica_el_lock_si_el_dia_no_cambia(self):
        resultado = self._turno(hora="09:00")
        assert resultado["success"]
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.valid_date, "11:00", business_id=1
        )

        assert reprogramado["success"], reprogramado
        assert self.lock_calls == [(1, appointments._appointment_day_ordinal(self.valid_date))]

    def test_admin_no_toma_locks_si_el_turno_no_existe(self):
        resultado = self._turno()
        self.lock_calls.clear()

        fallido = appointments.reschedule_appointment_admin(
            resultado["appointment_id"] + 999, self.otro_dia, "11:00", business_id=1
        )

        assert not fallido["success"]
        assert fallido["reason"] == "not_found"
        assert self.lock_calls == []


class TestRescheduleLockTimeoutYRowcount(_BaseLockDeReschedule):
    """`lock_timeout` agotado y UPDATE sin filas: nunca un éxito mentiroso."""

    @staticmethod
    def _timeout(connection, business_id, lock_key=0):
        import database.database as db

        raise db.sqlite3.OperationalError("canceling statement due to lock timeout")

    def test_lock_timeout_devuelve_busy_sin_mover_el_turno(self):
        resultado = self._turno()
        assert resultado["success"]
        antes = self._fila(resultado["appointment_id"])

        appointments.acquire_business_write_lock = self._timeout
        fallido = appointments.reschedule_appointment(
            resultado["appointment_id"],
            self.otro_dia,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )
        appointments.acquire_business_write_lock = self.original_lock

        assert not fallido["success"]
        assert fallido["reason"] == "busy"
        assert self._fila(resultado["appointment_id"]) == antes

    def test_lock_timeout_en_admin_devuelve_busy_sin_mover_el_turno(self):
        resultado = self._turno()
        assert resultado["success"]
        antes = self._fila(resultado["appointment_id"])

        appointments.acquire_business_write_lock = self._timeout
        fallido = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.otro_dia, "11:00", business_id=1
        )
        appointments.acquire_business_write_lock = self.original_lock

        assert not fallido["success"]
        assert fallido["reason"] == "busy"
        assert self._fila(resultado["appointment_id"]) == antes

    def test_update_sin_filas_no_devuelve_exito(self):
        resultado = self._turno()
        assert resultado["success"]

        appointments.get_connection = lambda: _ConexionRowcountCero(self.original_get_connection())
        fallido = appointments.reschedule_appointment(
            resultado["appointment_id"],
            self.otro_dia,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )
        appointments.get_connection = self.original_get_connection

        assert not fallido["success"], (
            "un UPDATE con rowcount 0 no puede reportarse como rescheduled"
        )
        assert fallido["reason"] == "not_found"
        fila = self._fila(resultado["appointment_id"])
        assert fila["appointment_date"].isoformat() == self.valid_date

    def test_update_sin_filas_en_admin_no_devuelve_exito(self):
        resultado = self._turno()
        assert resultado["success"]

        appointments.get_connection = lambda: _ConexionRowcountCero(self.original_get_connection())
        fallido = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.otro_dia, "11:00", business_id=1
        )
        appointments.get_connection = self.original_get_connection

        assert not fallido["success"]
        assert fallido["reason"] == "not_found"
        fila = self._fila(resultado["appointment_id"])
        assert fila["appointment_date"].isoformat() == self.valid_date


class TestEstadoAdminTomaElLockYTienePredicado(PostgreSQLTestCase):
    """`update_appointment_status_scoped` es la vía del panel admin."""

    def setup_method(self):
        self.valid_date = _next_open_day()
        self.original_lock = database.acquire_business_write_lock
        self.lock_calls = []
        database.acquire_business_write_lock = self._spy

    def teardown_method(self):
        database.acquire_business_write_lock = self.original_lock

    def _spy(self, connection, business_id, lock_key=0):
        self.lock_calls.append((business_id, lock_key))

    def _turno(self):
        return appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )

    def _estado(self, appointment_id):
        c = database.get_connection()
        try:
            return c.execute(
                "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()["status"]
        finally:
            c.close()

    def test_toma_el_lock_del_dia_del_turno(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        assert database.update_appointment_status_scoped(appointment_id, "cancelled", 1)

        assert self.lock_calls == [(1, appointments._appointment_day_ordinal(self.valid_date))]
        assert self._estado(appointment_id) == "cancelled"

    def test_no_toma_el_lock_si_el_turno_no_es_del_negocio(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        assert not database.update_appointment_status_scoped(appointment_id, "cancelled", 99)
        assert self.lock_calls == []
        assert self._estado(appointment_id) == "confirmed"

    def test_no_resucita_un_turno_cancelado(self):
        appointment_id = self._turno()["appointment_id"]
        assert database.update_appointment_status_scoped(appointment_id, "cancelled", 1)
        self.lock_calls.clear()

        assert not database.update_appointment_status_scoped(appointment_id, "confirmed", 1), (
            "cancelled -> confirmed es una resurrección y debe rechazarse"
        )
        assert self._estado(appointment_id) == "cancelled"

    def test_devuelve_false_si_el_estado_no_cambio(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        assert not database.update_appointment_status_scoped(appointment_id, "confirmed", 1), (
            "poner el estado que ya tiene no es un cambio"
        )
        assert self._estado(appointment_id) == "confirmed"

    def test_permite_corregir_desde_completed(self):
        appointment_id = self._turno()["appointment_id"]
        assert database.update_appointment_status_scoped(appointment_id, "completed", 1)
        assert database.update_appointment_status_scoped(appointment_id, "confirmed", 1), (
            "completed -> confirmed sigue siendo una corrección legítima del operador"
        )
        assert self._estado(appointment_id) == "confirmed"

    def test_rechaza_estados_desconocidos_sin_tocar_la_base(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        assert not database.update_appointment_status_scoped(appointment_id, "inventado", 1)
        assert self.lock_calls == []
        assert self._estado(appointment_id) == "confirmed"

    def test_lock_timeout_no_cambia_el_estado(self):
        appointment_id = self._turno()["appointment_id"]

        def _timeout(connection, business_id, lock_key=0):
            import database.database as db

            raise db.sqlite3.OperationalError("canceling statement due to lock timeout")

        database.acquire_business_write_lock = _timeout
        fallido = database.update_appointment_status_scoped(appointment_id, "cancelled", 1)
        database.acquire_business_write_lock = self.original_lock

        assert not fallido if not isinstance(fallido, dict) else not fallido["success"]
        assert self._estado(appointment_id) == "confirmed"
