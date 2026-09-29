import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import database.database as database
from services import appointments

FROZEN_NOW = datetime(
    2027, 1, 24, 10, 0
)  # domingo fijo: next_open_day() cae en lunes con turno de tarde

ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


class TestReservaDisponible(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_reserva_en_horario_disponible(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["appointment_id"])


class TestReservaOcupada(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_reserva_en_horario_ocupado(self):
        appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        result = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "09:00", 1
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")


class TestDomingoCerrado(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_reserva_en_domingo(self):
        sunday = datetime.now(ZONA_HORARIA).date()
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", sunday.isoformat(), "09:00", 1
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "closed_day")


class TestFechaInvalida(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_reserva_con_fecha_mal_formateada(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", "2025/08/18", "09:00", 1
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_date")

    def test_reserva_con_fecha_pasada(self):
        yesterday = (datetime.now(ZONA_HORARIA).date() - timedelta(days=1)).isoformat()
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", yesterday, "09:00", 1
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "past_date")


class TestCancelacionTelefonoCorrecto(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_cancelar_con_telefono_correcto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        appointment_id = result["appointment_id"]
        self.assertTrue(
            appointments.cancel_appointment(
                appointment_id,
                "3838439222",
                1,
                result["management_token"],
                customer_name="Ana Pérez",
            )
        )


class TestCancelacionTelefonoIncorrecto(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_cancelar_con_telefono_incorrecto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        appointment_id = result["appointment_id"]
        self.assertFalse(
            appointments.cancel_appointment(
                appointment_id, "3838439222", 1, "token_incorrecto", customer_name="Ana Pérez"
            )
        )


class TestReprogramacionHorarioOcupado(unittest.TestCase):
    def setUp(self):
        self._datetime_patch = mock.patch(f"{__name__}.datetime", _FrozenDatetime)
        self._datetime_patch.start()
        self.addCleanup(self._datetime_patch.stop)
        self._service_datetime_patch = mock.patch("services.appointments.datetime", _FrozenDatetime)
        self._service_datetime_patch.start()
        self.addCleanup(self._service_datetime_patch.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

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
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "occupied")


class TestDobleReservaSimultanea(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_doble_reserva_mismo_horario(self):
        result1 = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        result2 = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "09:00", 1
        )
        self.assertTrue(result1["success"])
        self.assertFalse(result2["success"])
        self.assertEqual(result2["reason"], "occupied")


class TestCancelacionTomaElLockDeEscritura(unittest.TestCase):
    """`cancel_appointment` debe(serializarse con creación/reprogramación.

    En SQLite el advisory lock es un no-op, así que se verifica la CONTRATACIÓN
    (que se pide el lock, con la misma clave `(business_id, día)` que usa
    `create_appointment`). La serialización real se prueba en
    `tests_pg/test_advisory_lock_live.py` contra PostgreSQL.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()
        self.original_lock = appointments.acquire_business_write_lock
        self.lock_calls = []
        appointments.acquire_business_write_lock = self._spy

    def tearDown(self):
        appointments.acquire_business_write_lock = self.original_lock
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _spy(self, connection, business_id, lock_key=0):
        self.lock_calls.append((business_id, lock_key))

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def test_cancelar_toma_el_lock_del_dia_del_turno(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.assertTrue(result["success"])
        self.lock_calls.clear()

        self.assertTrue(
            appointments.cancel_appointment(
                result["appointment_id"],
                "3838439222",
                1,
                result["management_token"],
                customer_name="Ana Pérez",
            )
        )

        esperado = (1, appointments._appointment_day_ordinal(self.valid_date))
        self.assertEqual(self.lock_calls, [esperado])

    def test_cancelar_no_toma_el_lock_si_el_turno_no_existe(self):
        resultado = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.lock_calls.clear()

        self.assertFalse(
            appointments.cancel_appointment(
                resultado["appointment_id"] + 999, "3838439222", 1, resultado["management_token"]
            )
        )
        self.assertEqual(self.lock_calls, [])

    def test_cancelar_toma_el_lock_una_sola_vez(self):
        """El turno se relee para la clave del lock y se revalida después."""
        resultado = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1
        )
        self.lock_calls.clear()

        appointments.cancel_appointment(
            resultado["appointment_id"], "3838439222", 1, resultado["management_token"]
        )

        self.assertEqual(len(self.lock_calls), 1)


class TestIdempotenciaDeReserva(unittest.TestCase):
    """`idempotency_key` hace que un reintento devuelva el turno original.

    Mismo patrón que `loyalty.redeem`: clave del cliente + índice único
    (business_id, idempotency_key) + lookup dentro del advisory lock.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    def _count(self):
        connection = database.get_connection()
        try:
            return connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        finally:
            connection.close()

    def _reservar(self, idempotency_key=None, hora="09:00", nombre="Ana Pérez"):
        return appointments.create_appointment(
            nombre, "3838439222", "Corte", self.valid_date, hora, 1, idempotency_key=idempotency_key
        )

    def test_reintento_devuelve_el_mismo_turno(self):
        primero = self._reservar("clave-abc")
        self.assertTrue(primero["success"])
        self.assertEqual(primero["reason"], "created")

        segundo = self._reservar("clave-abc")
        self.assertTrue(segundo["success"], "un reintento no debe fallar por 'occupied'")
        self.assertEqual(segundo["reason"], "already_created")
        self.assertTrue(segundo["idempotent_replay"])
        self.assertEqual(segundo["appointment_id"], primero["appointment_id"])
        self.assertEqual(self._count(), 1, "el reintento no debe crear un segundo turno")

    def test_el_replay_no_devuelve_el_management_token(self):
        primero = self._reservar("clave-abc")
        segundo = self._reservar("clave-abc")
        self.assertTrue(primero["management_token"])
        self.assertIsNone(
            segundo["management_token"], "el token no es reconstruible: solo se persiste su SHA-256"
        )

    def test_el_replay_conserva_los_datos_del_turno_original(self):
        primero = self._reservar("clave-abc")
        # Misma clave con payload DISTINTO: la clave identifica la operacion.
        segundo = self._reservar("clave-abc", hora="11:00", nombre="Bruno Gómez")
        self.assertTrue(segundo["success"])
        self.assertEqual(segundo["appointment_id"], primero["appointment_id"])
        self.assertEqual(segundo["appointment_time"], primero["appointment_time"])
        self.assertEqual(segundo["customer_name"], primero["customer_name"])
        self.assertEqual(self._count(), 1)

    def test_claves_distintas_crean_turnos_distintos(self):
        self.assertTrue(self._reservar("clave-1", hora="09:00")["success"])
        self.assertTrue(self._reservar("clave-2", hora="11:00")["success"])
        self.assertEqual(self._count(), 2)

    def test_sin_clave_el_comportamiento_es_el_historico(self):
        primero = self._reservar()
        self.assertTrue(primero["success"])
        self.assertNotIn("idempotent_replay", primero)
        self.assertEqual(primero["reason"], "created")

        segundo = self._reservar()
        self.assertFalse(segundo["success"])
        self.assertEqual(segundo["reason"], "occupied")

    def test_turnos_sin_clave_no_colisionan_en_el_indice(self):
        """NULL no participa del índice parcial: dos altas sin clave conviven."""
        self.assertTrue(self._reservar()["success"])
        self.assertTrue(self._reservar(hora="11:00")["success"])
        self.assertEqual(self._count(), 2)

    def test_la_clave_no_atreves_negocios(self):
        """El UNIQUE es (business_id, idempotency_key), no solo la clave."""
        primero = self._reservar("clave-compartida")
        self.assertTrue(primero["success"])
        # El negocio 2 no existe en esta base de pruebas: el scope por
        # business_id se verifica a nivel de indice, no de servicio.
        connection = database.get_connection()
        try:
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(appointments)")}
        finally:
            connection.close()
        self.assertIn("unique_appointment_idempotency_key", indexes)

    def test_relee_al_ganador_si_el_indice_unico_dispara(self):
        """La rama `IntegrityError` de la idempotencia relee al ganador.

        Es la carrera que el advisory lock por DÍA no puede serializar: dos
        requests con la misma clave en fechas distintas toman locks distintos,
        pasan ambas el SELECT de replay y compiten por el índice único. Aquí se
        oculta solo el PRIMER SELECT de replay (como si la otra transacción
        todavía no hubiera commiteado) para forzar la violación del índice.
        """
        ganador = self._reservar("clave-carrera")
        self.assertTrue(ganador["success"])

        real_get_connection = database.get_connection
        objetivo = appointments.get_connection

        class _ReplayCiego:
            """Delega todo salvo el primer SELECT de replay, que devuelve vacío."""

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

        self.assertTrue(
            replay["success"], "la UniqueViolation de idempotencia no debe ser 'occupied'"
        )
        self.assertEqual(replay["reason"], "already_created")
        self.assertEqual(replay["appointment_id"], ganador["appointment_id"])
        self.assertEqual(self._count(), 1)


class _CursorRowcountCero:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return 0

    def __getattr__(self, nombre):
        return getattr(self._cursor, nombre)


class _ConexionRowcountCero:
    """Proxy que fuerza rowcount=0 en el UPDATE, como si la fila ya no existiera.

    Simula la carrera que el `WHERE status = 'confirmed'` del UPDATE no puede
    observar dentro de su propia transacción: otra vía canceló el turno entre la
    revalidación y el UPDATE. Sirve para fijar que el servicio NO devuelve
    éxito cuando no se actualizó nada.
    """

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        cursor = self._connection.execute(sql, *args)
        if sql.lstrip().upper().startswith("UPDATE"):
            return _CursorRowcountCero(cursor)
        return cursor

    def __getattr__(self, nombre):
        return getattr(self._connection, nombre)


class _BaseLockDeReschedule(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()
        self.otro_dia = self._dia_laborable(
            datetime.strptime(self.valid_date, "%Y-%m-%d").date() + timedelta(days=1)
        )
        self.original_lock = appointments.acquire_business_write_lock
        self.original_get_connection = appointments.get_connection
        self.lock_calls = []
        appointments.acquire_business_write_lock = self._spy

    def tearDown(self):
        appointments.acquire_business_write_lock = self.original_lock
        appointments.get_connection = self.original_get_connection
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _spy(self, connection, business_id, lock_key=0):
        self.lock_calls.append((business_id, lock_key))

    @staticmethod
    def _dia_laborable(date):
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
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

    En SQLite el advisory lock es un no-op, así que se verifica la CONTRATACIÓN:
    que se piden los locks de origen y destino, con claves `(business_id, día)`
    idénticas a las de `create_appointment`/`cancel_appointment`, y en ORDEN
    ASCENDENTE. Ese orden es la razón por la que dos reprogramaciones
    concurrentes no pueden deadlockearse. La serialización real se prueba en
    `tests_pg/` contra PostgreSQL.
    """

    def test_toma_locks_de_origen_y_destino_en_orden_ascendente(self):
        resultado = self._turno()
        self.assertTrue(resultado["success"])
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment(
            resultado["appointment_id"],
            self.otro_dia,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )

        self.assertTrue(reprogramado["success"], reprogramado)
        self.assertEqual(self.lock_calls, self._locks_esperados(self.valid_date, self.otro_dia))

    def test_deduplica_el_lock_si_el_dia_no_cambia(self):
        resultado = self._turno(hora="09:00")
        self.assertTrue(resultado["success"])
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment(
            resultado["appointment_id"],
            self.valid_date,
            "11:00",
            "3838439222",
            business_id=1,
            management_token=resultado["management_token"],
        )

        self.assertTrue(reprogramado["success"], reprogramado)
        self.assertEqual(
            self.lock_calls,
            [(1, appointments._appointment_day_ordinal(self.valid_date))],
            "mismo día de origen y destino: un solo lock, no dos",
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

        self.assertFalse(fallido["success"])
        self.assertEqual(fallido["reason"], "not_found")
        self.assertEqual(self.lock_calls, [])

    def test_admin_toma_locks_de_origen_y_destino_en_orden_ascendente(self):
        resultado = self._turno()
        self.assertTrue(resultado["success"])
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.otro_dia, "11:00", business_id=1
        )

        self.assertTrue(reprogramado["success"], reprogramado)
        self.assertEqual(self.lock_calls, self._locks_esperados(self.valid_date, self.otro_dia))

    def test_admin_deduplica_el_lock_si_el_dia_no_cambia(self):
        resultado = self._turno(hora="09:00")
        self.assertTrue(resultado["success"])
        self.lock_calls.clear()

        reprogramado = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.valid_date, "11:00", business_id=1
        )

        self.assertTrue(reprogramado["success"], reprogramado)
        self.assertEqual(
            self.lock_calls, [(1, appointments._appointment_day_ordinal(self.valid_date))]
        )

    def test_admin_no_toma_locks_si_el_turno_no_existe(self):
        resultado = self._turno()
        self.lock_calls.clear()

        fallido = appointments.reschedule_appointment_admin(
            resultado["appointment_id"] + 999, self.otro_dia, "11:00", business_id=1
        )

        self.assertFalse(fallido["success"])
        self.assertEqual(fallido["reason"], "not_found")
        self.assertEqual(self.lock_calls, [])


class TestRescheduleLockTimeoutYRowcount(_BaseLockDeReschedule):
    """`lock_timeout` agotado y UPDATE sin filas: nunca un éxito mentiroso."""

    @staticmethod
    def _timeout(connection, business_id, lock_key=0):
        raise sqlite3.OperationalError("canceling statement due to lock timeout")

    def test_lock_timeout_devuelve_busy_sin_mover_el_turno(self):
        resultado = self._turno()
        self.assertTrue(resultado["success"])
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

        self.assertFalse(fallido["success"])
        self.assertEqual(fallido["reason"], "busy")
        self.assertEqual(self._fila(resultado["appointment_id"]), antes)

    def test_lock_timeout_en_admin_devuelve_busy_sin_mover_el_turno(self):
        resultado = self._turno()
        self.assertTrue(resultado["success"])
        antes = self._fila(resultado["appointment_id"])

        appointments.acquire_business_write_lock = self._timeout
        fallido = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.otro_dia, "11:00", business_id=1
        )
        appointments.acquire_business_write_lock = self.original_lock

        self.assertFalse(fallido["success"])
        self.assertEqual(fallido["reason"], "busy")
        self.assertEqual(self._fila(resultado["appointment_id"]), antes)

    def test_update_sin_filas_no_devuelve_exito(self):
        resultado = self._turno()
        self.assertTrue(resultado["success"])

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

        self.assertFalse(
            fallido["success"], "un UPDATE con rowcount 0 no puede reportarse como rescheduled"
        )
        self.assertEqual(fallido["reason"], "not_found")
        self.assertEqual(
            self._fila(resultado["appointment_id"])["appointment_date"],
            self.valid_date,
            "el turno no debe haberse movido",
        )

    def test_update_sin_filas_en_admin_no_devuelve_exito(self):
        resultado = self._turno()
        self.assertTrue(resultado["success"])

        appointments.get_connection = lambda: _ConexionRowcountCero(self.original_get_connection())
        fallido = appointments.reschedule_appointment_admin(
            resultado["appointment_id"], self.otro_dia, "11:00", business_id=1
        )
        appointments.get_connection = self.original_get_connection

        self.assertFalse(fallido["success"])
        self.assertEqual(fallido["reason"], "not_found")
        self.assertEqual(
            self._fila(resultado["appointment_id"])["appointment_date"], self.valid_date
        )


class TestEstadoAdminTomaElLockYTienePredicado(unittest.TestCase):
    """`update_appointment_status_scoped` es la vía del panel admin.

    Debe entrar en la misma serialización del calendario que crear/cancelar/
    reprogramar, y no debe poder resucitar un turno que el cliente canceló.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()
        self.original_lock = database.acquire_business_write_lock
        self.lock_calls = []
        database.acquire_business_write_lock = self._spy

    def tearDown(self):
        database.acquire_business_write_lock = self.original_lock
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _spy(self, connection, business_id, lock_key=0):
        self.lock_calls.append((business_id, lock_key))

    @staticmethod
    def _next_open_day():
        date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()

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

        self.assertTrue(database.update_appointment_status_scoped(appointment_id, "cancelled", 1))

        self.assertEqual(
            self.lock_calls, [(1, appointments._appointment_day_ordinal(self.valid_date))]
        )
        self.assertEqual(self._estado(appointment_id), "cancelled")

    def test_no_toma_el_lock_si_el_turno_no_es_del_negocio(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        self.assertFalse(database.update_appointment_status_scoped(appointment_id, "cancelled", 99))
        self.assertEqual(self.lock_calls, [])
        self.assertEqual(self._estado(appointment_id), "confirmed")

    def test_no_resucita_un_turno_cancelado(self):
        appointment_id = self._turno()["appointment_id"]
        self.assertTrue(database.update_appointment_status_scoped(appointment_id, "cancelled", 1))
        self.lock_calls.clear()

        self.assertFalse(
            database.update_appointment_status_scoped(appointment_id, "confirmed", 1),
            "cancelled -> confirmed es una resurrección y debe rechazarse",
        )
        self.assertEqual(self._estado(appointment_id), "cancelled")

    def test_devuelve_false_si_el_estado_no_cambio(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        self.assertFalse(
            database.update_appointment_status_scoped(appointment_id, "confirmed", 1),
            "poner el estado que ya tiene no es un cambio",
        )
        self.assertEqual(self._estado(appointment_id), "confirmed")

    def test_permite_corregir_desde_completed(self):
        appointment_id = self._turno()["appointment_id"]
        self.assertTrue(database.update_appointment_status_scoped(appointment_id, "completed", 1))
        self.assertTrue(
            database.update_appointment_status_scoped(appointment_id, "confirmed", 1),
            "completed -> confirmed sigue siendo una corrección legítima del operador",
        )
        self.assertEqual(self._estado(appointment_id), "confirmed")

    def test_rechaza_estados_desconocidos_sin_tocar_la_base(self):
        appointment_id = self._turno()["appointment_id"]
        self.lock_calls.clear()

        self.assertFalse(database.update_appointment_status_scoped(appointment_id, "inventado", 1))
        self.assertEqual(self.lock_calls, [])
        self.assertEqual(self._estado(appointment_id), "confirmed")

    def test_lock_timeout_no_cambia_el_estado(self):
        appointment_id = self._turno()["appointment_id"]

        def _timeout(connection, business_id, lock_key=0):
            raise sqlite3.OperationalError("canceling statement due to lock timeout")

        database.acquire_business_write_lock = _timeout
        fallido = database.update_appointment_status_scoped(appointment_id, "cancelled", 1)
        database.acquire_business_write_lock = self.original_lock

        self.assertFalse(fallido["success"] if isinstance(fallido, dict) else fallido)
        self.assertEqual(self._estado(appointment_id), "confirmed")


if __name__ == "__main__":
    unittest.main()
