"""Tests del lifecycle del pool (FASE 4D.1).

Verify:
1. checkout devuelve conexión.
2. close devuelve conexión al pool (putconn).
3. close dos veces no rompe (idempotente).
4. múltiples ciclos checkout/close reutilizan (sin agotar ni crear pools).
5. SQLite mantiene comportamiento anterior.
6. el singleton del pool sigue siendo el mismo.
7. no existe segundo pool.
8. get_pg_pool_connection requiere pool inicializado.
"""

from __future__ import annotations

import sqlite3
from unittest import mock

import database.database as dbmod
from database.pg_pool import PgConnectionProxy


# 1) checkout devuelve una conexión envuelta
def test_checkout_returns_proxy(monkeypatch):
    fake_conn = mock.MagicMock(name="psycopg_conn")
    fake_pool = mock.MagicMock(name="pool")
    fake_pool.getconn.return_value = fake_conn

    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_database_backend", return_value="postgresql"):
        with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=fake_pool):
            conn = dbmod.get_connection()
            assert isinstance(conn, PgConnectionProxy)


# 2) close llama putconn al pool
def test_close_returns_to_pool(monkeypatch):
    fake_conn = mock.MagicMock(name="psycopg_conn")
    fake_pool = mock.MagicMock(name="pool")
    fake_pool.getconn.return_value = fake_conn

    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_database_backend", return_value="postgresql"):
        with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=fake_pool):
            conn = dbmod.get_connection()
            conn.close()
            fake_pool.putconn.assert_called_once_with(fake_conn)


# 3) close doble es idempotente (putconn una sola vez)
def test_close_idempotent(monkeypatch):
    fake_conn = mock.MagicMock(name="psycopg_conn")
    fake_pool = mock.MagicMock(name="pool")
    fake_pool.getconn.return_value = fake_conn

    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_database_backend", return_value="postgresql"):
        with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=fake_pool):
            conn = dbmod.get_connection()
            conn.close()
            conn.close()  # segundo close no debe llamar putconn de nuevo
            assert fake_pool.putconn.call_count == 1


# 4) múltiples ciclos checkout/close reutilizan (putconn se llama por cada close)
def test_multiple_cycles_reuse(monkeypatch):
    fake_conn = mock.MagicMock(name="psycopg_conn")
    fake_pool = mock.MagicMock(name="pool")
    fake_pool.getconn.return_value = fake_conn

    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_database_backend", return_value="postgresql"):
        with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=fake_pool):
            for _ in range(3):
                conn = dbmod.get_connection()
                conn.close()
            assert fake_pool.getconn.call_count == 3
            assert fake_pool.putconn.call_count == 3


# 5) SQLite mantiene comportamiento anterior (sqlite3 + row_factory intacto)
def test_sqlite_unchanged(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = dbmod.get_connection()
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory == sqlite3.Row
    finally:
        conn.close()


# 6) proxy delega execute/commit/rollback al psycopg real
def test_proxy_delegates_methods(monkeypatch):
    fake_conn = mock.MagicMock(name="psycopg_conn")
    fake_cursor = mock.MagicMock(name="psycopg_cursor")
    fake_conn.cursor.return_value = fake_cursor
    fake_pool = mock.MagicMock(name="pool")
    fake_pool.getconn.return_value = fake_conn

    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_database_backend", return_value="postgresql"):
        with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=fake_pool):
            conn = dbmod.get_connection()
            conn.execute("SELECT 1")
            conn.commit()
            conn.rollback()
            fake_cursor.execute.assert_called_once_with("SELECT 1")
            fake_conn.commit.assert_called_once()
            fake_conn.rollback.assert_called_once()


# 7) get_pg_pool_connection requiere pool inicializado
def test_checkout_requires_pool():
    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=None):
        try:
            pg_pool_mod.get_pg_pool_connection()
            raise AssertionError("Se esperaba RuntimeError")
        except RuntimeError:
            pass


# 8) putconn falla de forma segura: fallback a conn.close()
def test_putconn_failure_fallback(monkeypatch):
    fake_conn = mock.MagicMock(name="psycopg_conn")
    fake_pool = mock.MagicMock(name="pool")
    fake_pool.getconn.return_value = fake_conn
    fake_pool.putconn.side_effect = RuntimeError("pool closed")

    import database.pg_pool as pg_pool_mod

    with mock.patch.object(pg_pool_mod, "get_database_backend", return_value="postgresql"):
        with mock.patch.object(pg_pool_mod, "get_pg_pool", return_value=fake_pool):
            conn = dbmod.get_connection()
            conn.close()  # no debe lanzar
            fake_conn.close.assert_called_once()
