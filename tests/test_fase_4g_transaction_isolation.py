"""Tests de aislamiento transaccional y concurrencia (Fase 4G).

Cubre, de forma determinista y sin servidor PostgreSQL:
- Traducción de errores psycopg3 -> excepciones sqlite3 en el proxy.
- Intercept de comandos de transacción (BEGIN IMMEDIATE / COMMIT / ROLLBACK)
  para no romper la desambiguación READ COMMITTED de PostgreSQL.
- Helper de advisory lock por negocio (no-op sobre SQLite).
- Guardas de negocio nuevas: UPDATE condicional con rowcount en redeem,
  acreditación idempotente multi-turno, sesiones get_or_create.

La validación contra PostgreSQL REAL queda pendiente (no hay servidor en el
entorno); estos tests solo fijan el comportamiento determinista del seam y de
la capa de negocio sobre SQLite.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from psycopg import errors as psycopg_errors
from psycopg.pq import TransactionStatus

import database.database as database
from database import pg_pool
from services import loyalty
from services.conversations import get_or_create_conversation_session_scoped


def _next_open_weekday():
    day = datetime.now().date() + timedelta(days=1)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return day.isoformat()


def _failing_cursor_proxy(error):
    mock_cursor = mock.MagicMock()
    mock_cursor.execute.side_effect = error
    return pg_pool.PgCursorProxy(mock_cursor)


class TestPgErrorTranslation(unittest.TestCase):
    """psycopg3 lanza psycopg.errors.*; el contrato de la app captura sqlite3.*."""

    def test_unique_violation_se_traduce_a_integrity_error(self):
        proxy = _failing_cursor_proxy(psycopg_errors.UniqueViolation("duplicado"))
        with self.assertRaises(sqlite3.IntegrityError):
            proxy.execute("INSERT INTO businesses (name) VALUES (?)", ("dup",))

    def test_foreign_key_violation_se_traduce_a_integrity_error(self):
        proxy = _failing_cursor_proxy(psycopg_errors.ForeignKeyViolation("fk violada"))
        with self.assertRaises(sqlite3.IntegrityError):
            proxy.execute("DELETE FROM businesses WHERE id = ?", (1,))

    def test_operational_error_se_traduce_a_operational_error(self):
        proxy = _failing_cursor_proxy(psycopg_errors.OperationalError("conn caída"))
        with self.assertRaises(sqlite3.OperationalError):
            proxy.execute("UPDATE businesses SET name = ?", ("x",))


class _FakePgInfo:
    def __init__(self, transaction_status):
        self.transaction_status = transaction_status


class _FakePgConnection:
    def __init__(self, transaction_status):
        self.info = _FakePgInfo(transaction_status)
        self.begun = 0
        self.committed = 0
        self.rolled_back = 0

    def begin(self):
        self.begun += 1

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class TestTransactionCommandHandling(unittest.TestCase):
    """BEGIN IMMEDIATE no debe reintentar un BEGIN cuando ya hay transacción."""

    def test_begin_immediate_idle_abre_transaccion(self):
        conn = _FakePgConnection(TransactionStatus.IDLE)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "BEGIN IMMEDIATE"))
        self.assertEqual(conn.begun, 1)

    def test_begin_immediate_en_transaccion_es_noop(self):
        conn = _FakePgConnection(TransactionStatus.INTRANS)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "BEGIN IMMEDIATE"))
        self.assertEqual(conn.begun, 0)

    def test_begin_immediate_tras_estado_inerror_rollback_y_begin(self):
        conn = _FakePgConnection(TransactionStatus.INERROR)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "BEGIN IMMEDIATE"))
        self.assertEqual(conn.rolled_back, 1)
        self.assertEqual(conn.begun, 1)

    def test_rollback_en_transaccion(self):
        conn = _FakePgConnection(TransactionStatus.INTRANS)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "ROLLBACK"))
        self.assertEqual(conn.rolled_back, 1)

    def test_rollback_idle_es_noop(self):
        conn = _FakePgConnection(TransactionStatus.IDLE)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "ROLLBACK"))
        self.assertEqual(conn.rolled_back, 0)

    def test_commit_en_transaccion(self):
        conn = _FakePgConnection(TransactionStatus.INTRANS)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "COMMIT"))
        self.assertEqual(conn.committed, 1)

    def test_commit_idle_es_noop(self):
        conn = _FakePgConnection(TransactionStatus.IDLE)
        self.assertTrue(pg_pool.handle_transaction_command(conn, "COMMIT"))
        self.assertEqual(conn.committed, 0)

    def test_consultas_normales_no_se_tratan(self):
        conn = _FakePgConnection(TransactionStatus.IDLE)
        self.assertFalse(pg_pool.handle_transaction_command(conn, "UPDATE x SET y = 1"))

    def test_connection_proxy_begin_immediate_delega_en_begin(self):
        conn = _FakePgConnection(TransactionStatus.IDLE)
        conn.cursor = mock.MagicMock(return_value=mock.MagicMock())
        proxy = pg_pool.PgConnectionProxy(conn, mock.MagicMock())
        proxy.execute("BEGIN IMMEDIATE")
        self.assertEqual(conn.begun, 1)

    def test_connection_proxy_executescript_no_doble_begin(self):
        conn = _FakePgConnection(TransactionStatus.INTRANS)
        conn.cursor = mock.MagicMock(return_value=mock.MagicMock())
        proxy = pg_pool.PgConnectionProxy(conn, mock.MagicMock())
        proxy.execute("BEGIN IMMEDIATE")
        proxy.execute("UPDATE x SET y = 1")
        self.assertEqual(conn.begun, 0)


class TestBusinessWriteLock(unittest.TestCase):
    """advisory xact lock por (business_id, lock_key); no-op sobre SQLite."""

    def setUp(self):
        self._backend = mock.patch.object(database, "get_backend", return_value="postgresql")
        self._backend.start()

    def tearDown(self):
        self._backend.stop()

    def test_advisory_lock_se_ejecuta_en_postgresql(self):
        fake_conn = mock.MagicMock()
        database.acquire_business_write_lock(fake_conn, 7, 123)
        fake_conn.execute.assert_called_once_with("SELECT pg_advisory_xact_lock(?, ?)", (7, 123))

    def test_advisory_lock_valida_business_id(self):
        with self.assertRaises(ValueError):
            database.acquire_business_write_lock(mock.MagicMock(), 0, 1)

    def test_advisory_lock_noop_en_sqlite(self):
        fake_conn = mock.MagicMock()
        with mock.patch.object(database, "get_backend", return_value="sqlite"):
            database.acquire_business_write_lock(fake_conn, 7, 123)
        fake_conn.execute.assert_not_called()

    def test_adapt_advisory_lock_placeholders(self):
        sql, params = pg_pool.adapt_query_for_postgres(
            "SELECT pg_advisory_xact_lock(?, ?)", (7, 123)
        )
        self.assertEqual(sql, "SELECT pg_advisory_xact_lock(%s, %s)")
        self.assertEqual(params, (7, 123))


class TestLoyaltyRaceGuards(unittest.TestCase):
    """Guardas nuevas sobre la capa de negocio (SQLite determinista)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self._tmp.name) / "appointments.db"
        database.init_database()
        self.assertEqual(database.update_loyalty_settings_scoped(1, True, 10), True)

    def tearDown(self):
        database.DATABASE_PATH = self._original_database_path
        self._tmp.cleanup()

    def _execute(self, sql, params=()):
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

    def _create_reward(self, points_cost=60):
        result = loyalty.save_reward(1, None, "Premio", "descripción", points_cost, True)
        self.assertEqual(result["success"], True)
        return self._query("SELECT id FROM rewards WHERE name = 'Premio' AND business_id = 1")[0][
            "id"
        ]

    def _insert_appointment(self, date_, time_, status="completed", phone="3815000001"):
        end_min = (int(time_[:2]) * 60 + int(time_[3:])) + 60
        end_hhmm = f"{end_min // 60:02d}:{end_min % 60:02d}"
        connection = database.get_connection()
        try:
            cur = connection.execute(
                "INSERT INTO appointments "
                "(customer_name, phone, customer_email, service, appointment_date, "
                " appointment_time, appointment_end, duration, status, business_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 60, ?, 1)",
                ("Cliente", phone, None, "Corte", date_, time_, end_hhmm, status),
            )
            connection.commit()
            return cur.lastrowid
        finally:
            connection.close()

    def test_redeem_insufficient_cuando_saldo_columnar_queda_por_debajo(self):
        """Rama nueva: UPDATE condicional con rowcount != 1 => rollback e insuficiente.

        Simula la carrera: el SUM del ledger pasa (éste es autoritativo) pero la
        columna points_balance ya quedó por debajo del costo.
        """
        account_id = database.get_or_create_loyalty_account_scoped(1, "3815000001")[0]["id"]
        self._execute(
            "INSERT INTO points_ledger "
            "(business_id, account_id, delta, type, reason, points_per_completed) "
            "VALUES (1, ?, 100, 'earn', 'test', 10)",
            (account_id,),
        )
        self._execute("UPDATE loyalty_accounts SET points_balance = 0 WHERE id = ?", (account_id,))
        reward_id = self._create_reward(60)

        result = loyalty.redeem(1, account_id, reward_id, "key-race")

        self.assertEqual(result, {"success": False, "reason": "insufficient_points"})
        total = self._query(
            "SELECT COALESCE(SUM(delta), 0) AS t FROM points_ledger "
            "WHERE business_id = 1 AND account_id = ?",
            (account_id,),
        )[0]["t"]
        self.assertEqual(total, 100)
        self.assertEqual(len(self._query("SELECT id FROM redemptions WHERE business_id = 1")), 0)

    def test_redeem_misma_key_devuelve_already_processed(self):
        account_id = database.get_or_create_loyalty_account_scoped(1, "3815000001")[0]["id"]
        self._execute(
            "INSERT INTO points_ledger "
            "(business_id, account_id, delta, type, reason, points_per_completed) "
            "VALUES (1, ?, 100, 'earn', 'test', 10)",
            (account_id,),
        )
        self._execute(
            "UPDATE loyalty_accounts SET points_balance = 100 WHERE id = ?", (account_id,)
        )
        reward_id = self._create_reward(60)

        first = loyalty.redeem(1, account_id, reward_id, "key-dup")
        second = loyalty.redeem(1, account_id, reward_id, "key-dup")

        self.assertEqual(first["success"], True)
        self.assertEqual(second["success"], True)
        self.assertEqual(second["reason"], "already_processed")
        redeem_rows = self._query(
            "SELECT id FROM redemptions WHERE business_id = 1 AND idempotency_key = 'key-dup'"
        )
        self.assertEqual(len(redeem_rows), 1)

    def test_award_dos_turnos_mismo_phone_una_cuenta_dos_earn(self):
        day = _next_open_weekday()
        apt1 = self._insert_appointment(day, "10:00")
        apt2 = self._insert_appointment(day, "11:00")

        r1 = loyalty.award_points_for_completed(1, apt1)
        r2 = loyalty.award_points_for_completed(1, apt2)

        self.assertEqual(r1["success"], True)
        self.assertEqual(r2["success"], True)
        accounts = self._query(
            "SELECT id FROM loyalty_accounts "
            "WHERE business_id = 1 AND customer_phone = '3815000001'"
        )
        self.assertEqual(len(accounts), 1)
        account_id = accounts[0]["id"]
        earns = self._query(
            "SELECT id FROM points_ledger "
            "WHERE business_id = 1 AND account_id = ? AND type = 'earn' "
            "AND appointment_id IS NOT NULL",
            (account_id,),
        )
        self.assertEqual(len(earns), 2)
        balance = self._query(
            "SELECT points_balance FROM loyalty_accounts WHERE id = ?", (account_id,)
        )[0]["points_balance"]
        self.assertEqual(balance, 20)


class TestConversationSessionRace(unittest.TestCase):
    """Sesión get_or_create sigue creando UNA fila por (business, phone)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self._tmp.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self._original_database_path
        self._tmp.cleanup()

    def test_doble_get_or_create_una_sola_sesion(self):
        s1 = get_or_create_conversation_session_scoped(1, "3815000001", "Cliente")
        s2 = get_or_create_conversation_session_scoped(1, "3815000001", "Cliente")
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)
        connection = database.get_connection()
        try:
            rows = connection.execute(
                "SELECT id FROM conversation_sessions "
                "WHERE business_id = 1 AND customer_phone = '3815000001'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
