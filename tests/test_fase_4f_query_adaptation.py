"""Tests de adaptación de queries SQL y proxies para PostgreSQL (Fase 4F)."""

import unittest
from unittest import mock

from database.pg_pool import (
    PgConnectionProxy,
    PgCursorProxy,
    PgRowProxy,
    adapt_query_for_postgres,
    escape_literal_percent,
    replace_placeholders,
)


class TestFase4FQueryAdaptation(unittest.TestCase):
    def test_replace_placeholders_outside_quotes(self):
        """Verifica que '?' se reemplace por '%s' fuera de comillas simples y dobles."""
        self.assertEqual(
            replace_placeholders("SELECT * FROM users WHERE id = ? AND email = ?"),
            "SELECT * FROM users WHERE id = %s AND email = %s",
        )
        self.assertEqual(
            replace_placeholders("SELECT * FROM knowledge WHERE question LIKE '%?' AND active = ?"),
            "SELECT * FROM knowledge WHERE question LIKE '%?' AND active = %s",
        )
        self.assertEqual(
            replace_placeholders('SELECT * FROM knowledge WHERE question = "?" AND id = ?'),
            'SELECT * FROM knowledge WHERE question = "?" AND id = %s',
        )

    def test_adapt_pragma(self):
        """Verifica que las sentencias PRAGMA se ignoren para PostgreSQL."""
        sql, params = adapt_query_for_postgres("PRAGMA busy_timeout = 10000")
        self.assertIsNone(sql)
        self.assertIsNone(params)

    def test_adapt_begin_immediate(self):
        """Verifica que BEGIN IMMEDIATE se adapte a BEGIN."""
        sql, params = adapt_query_for_postgres("BEGIN IMMEDIATE")
        self.assertEqual(sql, "BEGIN")

    def test_adapt_insert_or_ignore(self):
        """Verifica la adaptación de INSERT OR IGNORE a ON CONFLICT DO NOTHING."""
        sql, _ = adapt_query_for_postgres(
            "INSERT OR IGNORE INTO notification_log (a, b) VALUES (?, ?)"
        )
        self.assertEqual(
            sql, "INSERT INTO notification_log (a, b) VALUES (%s, %s) ON CONFLICT DO NOTHING"
        )

    def test_adapt_insert_or_replace_schema_version(self):
        """Verifica la adaptación de INSERT OR REPLACE a ON CONFLICT DO UPDATE."""
        sql, _ = adapt_query_for_postgres(
            "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)"
        )
        self.assertEqual(
            sql,
            "INSERT INTO schema_version (id, version) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version",
        )

    def test_adapt_datetime_now(self):
        """Verifica la adaptación de datetime('now') y datetime('now', ?)."""
        sql, _ = adapt_query_for_postgres(
            "UPDATE invitations SET used_at = datetime('now') WHERE id = ?"
        )
        self.assertEqual(sql, "UPDATE invitations SET used_at = CURRENT_TIMESTAMP WHERE id = %s")

        sql2, _ = adapt_query_for_postgres(
            "SELECT * FROM log WHERE last_attempt_at < datetime('now', ?)"
        )
        self.assertEqual(
            sql2, "SELECT * FROM log WHERE last_attempt_at < (CURRENT_TIMESTAMP + (%s)::interval)"
        )

    def test_escape_literal_percent(self):
        """Verifica que '%' literales se dupliquen preservando placeholders y '%%'."""
        self.assertEqual(escape_literal_percent("SELECT '%' AS pct"), "SELECT '%%' AS pct")
        self.assertEqual(
            escape_literal_percent("LIKE '%' || col || '%' WHERE id = %s"),
            "LIKE '%%' || col || '%%' WHERE id = %s",
        )
        self.assertEqual(escape_literal_percent("a%%b %s %t %b"), "a%%b %s %t %b")
        self.assertEqual(escape_literal_percent("SELECT '%'"), "SELECT '%%'")

    def test_adapt_like_percent_literal_in_parametrized_query(self):
        """Verifica que la consulta LIKE '%' || col || '%' sea válida para psycopg3."""
        sql, params = adapt_query_for_postgres(
            """
            SELECT ca.question_text
            FROM conversation_analytics ca
            LEFT JOIN business_knowledge bk
                ON bk.business_id = ca.business_id
                AND bk.active = 1
                AND (bk.question LIKE '%' || ca.question_text || '%'
                     OR ca.question_text LIKE '%' || bk.question || '%')
            WHERE ca.business_id = ?
                AND bk.id IS NULL
            ORDER BY ca.count DESC
            LIMIT ?
            """,
            (1, 20),
        )
        self.assertEqual(sql.count("LIKE '%%' ||"), 2)
        self.assertIn("business_id = %s", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params, (1, 20))

    def test_pg_row_proxy_contracts(self):
        """Verifica los contratos de PgRowProxy: clave, índice entero, dict, get."""
        keys = ("id", "name", "price")
        values = (42, "Corte de pelo", 10000.0)
        row = PgRowProxy(keys, values)

        # Acceso por clave
        self.assertEqual(row["id"], 42)
        self.assertEqual(row["name"], "Corte de pelo")
        self.assertEqual(row["price"], 10000.0)

        # Acceso por índice entero (compatibilidad con fetchone()[0])
        self.assertEqual(row[0], 42)
        self.assertEqual(row[1], "Corte de pelo")
        self.assertEqual(row[2], 10000.0)

        # Método .get()
        self.assertEqual(row.get("name"), "Corte de pelo")
        self.assertIsNone(row.get("missing"))
        self.assertEqual(row.get("missing", "default"), "default")

        # Conversión a dict nativo
        d = dict(row)
        self.assertEqual(d, {"id": 42, "name": "Corte de pelo", "price": 10000.0})

        # Iteración sobre llaves
        self.assertEqual(list(row), ["id", "name", "price"])
        self.assertEqual(tuple(row.keys()), ("id", "name", "price"))
        self.assertEqual(tuple(row.values()), (42, "Corte de pelo", 10000.0))

    def test_pg_cursor_proxy_select_and_fetch(self):
        """Verifica el funcionamiento de PgCursorProxy para SELECT fetchone/fetchall."""
        mock_cursor = mock.MagicMock()
        mock_cursor.description = [("id", None), ("email", None)]
        mock_cursor.fetchone.return_value = (10, "user@example.com")
        mock_cursor.fetchall.return_value = [(10, "user@example.com"), (11, "other@example.com")]

        proxy = PgCursorProxy(mock_cursor)
        proxy.execute("SELECT id, email FROM users WHERE id = ?", (10,))

        mock_cursor.execute.assert_called_once_with(
            "SELECT id, email FROM users WHERE id = %s", (10,)
        )

        row = proxy.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], 10)
        self.assertEqual(row[0], 10)
        self.assertEqual(row["email"], "user@example.com")

        rows = proxy.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["id"], 11)
        self.assertEqual(rows[1][1], "other@example.com")

    def test_pg_cursor_proxy_insert_lastrowid_returning(self):
        """Verifica que PgCursorProxy añada RETURNING id en INSERT y capture lastrowid."""
        mock_cursor = mock.MagicMock()
        mock_cursor.fetchone.return_value = {"id": 99}

        proxy = PgCursorProxy(mock_cursor)
        proxy.execute("INSERT INTO users (email) VALUES (?)", ("new@example.com",))

        # Debe añadir RETURNING id y cambiar ? a %s
        mock_cursor.execute.assert_called_once_with(
            "INSERT INTO users (email) VALUES (%s) RETURNING id", ("new@example.com",)
        )
        self.assertEqual(proxy.lastrowid, 99)

    def test_pg_connection_proxy_execution_flow(self):
        """Verifica que PgConnectionProxy gestione execute(), commit(), rollback() y close()."""
        mock_conn = mock.MagicMock()
        mock_cursor = mock.MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_pool = mock.MagicMock()

        conn_proxy = PgConnectionProxy(mock_conn, mock_pool)

        # execute() retorna PgCursorProxy
        cur_proxy = conn_proxy.execute("UPDATE users SET active = 1 WHERE id = ?", (5,))
        self.assertIsInstance(cur_proxy, PgCursorProxy)

        conn_proxy.commit()
        mock_conn.commit.assert_called_once()

        conn_proxy.rollback()
        mock_conn.rollback.assert_called_once()

        # close() devuelve la conexión al pool (putconn)
        conn_proxy.close()
        mock_pool.putconn.assert_called_once_with(mock_conn)


if __name__ == "__main__":
    unittest.main()
