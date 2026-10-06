"""Tests de adaptación de queries SQL y proxies para PostgreSQL (Fase 4F)."""

import unittest
import uuid
from unittest import mock

from database.database import create_user_scoped, get_connection, revoke_all_sessions_scoped
from database.pg_pool import (
    PgConnectionProxy,
    PgCursorProxy,
    PgRowProxy,
    _adapt_insert_boolean_values,
    _adapt_update_boolean_params,
    adapt_query_for_postgres,
    escape_literal_percent,
    replace_placeholders,
    split_sql_statements,
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

    def test_split_sql_statements_basic(self):
        """Verifica división básica por ';'."""
        sql = "CREATE TABLE a (id int); CREATE TABLE b (id int);"
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertIn("CREATE TABLE a", stmts[0])
        self.assertIn("CREATE TABLE b", stmts[1])

    def test_split_sql_statements_respects_string_literals(self):
        """Verifica que ';' dentro de comillas simples no separa sentencias."""
        sql = "INSERT INTO x (col) VALUES ('a;b'); CREATE TABLE y (id int);"
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertIn("VALUES ('a;b')", stmts[0])
        self.assertIn("CREATE TABLE y", stmts[1])

    def test_split_sql_statements_escaped_quotes(self):
        """Verifica que comillas escapadas '' dentro de literales no cierran el string."""
        sql = "INSERT INTO x (col) VALUES ('it''s a test');"
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 1)
        self.assertIn("it''s a test", stmts[0])

    def test_split_sql_statements_skips_comments(self):
        """Verifica que comentarios -- y /* */ se ignoren."""
        sql = """
        -- comentario de línea
        CREATE TABLE a (id int);
        /* comentario de bloque */
        CREATE TABLE b (id int);
        """
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertIn("CREATE TABLE a", stmts[0])
        self.assertIn("CREATE TABLE b", stmts[1])

    def test_split_sql_statements_trailing_without_semicolon(self):
        """Verifica que la última sentencia sin ';' se incluye."""
        sql = "CREATE TABLE a (id int); CREATE TABLE b (id int)"
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 2)

    def test_split_sql_statements_begin_commit(self):
        """Verifica que BEGIN y COMMIT se manejen como sentencias separadas."""
        sql = "BEGIN; CREATE TABLE a (id int); COMMIT;"
        stmts = split_sql_statements(sql)
        self.assertEqual(len(stmts), 3)
        self.assertEqual(stmts[0].upper(), "BEGIN")
        self.assertIn("CREATE TABLE a", stmts[1])
        self.assertEqual(stmts[2].upper(), "COMMIT")

    def test_adapt_insert_boolean_values_true_false(self):
        """Convierte 0/1 a false/true en columnas BOOLEAN de INSERT VALUES."""
        sql = (
            "INSERT INTO loyalty_settings "
            "(business_id, enabled, points_per_completed_appointment) "
            "VALUES (?, 0, 1)"
        )
        result_sql, _ = _adapt_insert_boolean_values(sql)
        self.assertIn("FALSE", result_sql.upper())
        self.assertIn("1)", result_sql)  # points_per_completed_appointment keeps integer 1

    def test_adapt_insert_boolean_values_no_boolean_column(self):
        """No modifica INSERT VALUES sin columnas booleanas."""
        sql = "INSERT INTO services (name, price, duration) VALUES (?, 100, 60)"
        result_sql, result_params = _adapt_insert_boolean_values(sql)
        self.assertEqual(result_sql, sql)
        self.assertIsNone(result_params)

    def test_adapt_insert_boolean_values_not_insert(self):
        """No afecta queries que no son INSERT."""
        sql = "SELECT * FROM businesses WHERE active"
        result_sql, result_params = _adapt_insert_boolean_values(sql)
        self.assertEqual(result_sql, sql)
        self.assertIsNone(result_params)

    def test_adapt_insert_boolean_values_multi_fila_literales(self):
        """Un VALUES multi-fila convierte el 0/1 de TODAS las filas, no solo la primera."""
        sql = (
            "INSERT INTO weekly_schedules (day_of_week, is_open, business_id) "
            "VALUES (0, 1, 2), (6, 0, 2)"
        )
        result_sql, _ = _adapt_insert_boolean_values(sql)
        self.assertEqual(
            result_sql,
            "INSERT INTO weekly_schedules (day_of_week, is_open, business_id) "
            "VALUES (0, true, 2), (6, false, 2)",
        )

    def test_adapt_insert_boolean_values_multi_fila_placeholders(self):
        """Los params booleanos de todas las filas se convierten y no se desalinean."""
        sql = "INSERT INTO sessions (user_id, revoked) VALUES (?, ?), (?, ?)"
        result_sql, result_params = _adapt_insert_boolean_values(sql, (1, 1, 2, 0))
        self.assertEqual(
            result_sql, "INSERT INTO sessions (user_id, revoked) VALUES (?, ?), (?, ?)"
        )
        self.assertEqual(result_params, (1, True, 2, False))

    def test_adapt_insert_boolean_values_una_fila_conserva_semantica(self):
        """Una sola fila conserva exactamente el comportamiento previo."""
        sql = "INSERT INTO sessions (user_id, token_hash, revoked) VALUES (?, 'abc', 0)"
        result_sql, result_params = _adapt_insert_boolean_values(sql, (7,))
        self.assertEqual(
            result_sql,
            "INSERT INTO sessions (user_id, token_hash, revoked) VALUES (?, 'abc', false)",
        )
        self.assertEqual(result_params, (7,))

    def test_adapt_insert_boolean_values_multi_fila_no_toca_no_booleanas(self):
        """Las columnas no booleanas conservan su entero en todas las filas."""
        sql = "INSERT INTO services (name, duration, active) VALUES ('A', 30, 1), ('B', 60, 0)"
        result_sql, _ = _adapt_insert_boolean_values(sql)
        self.assertEqual(
            result_sql,
            "INSERT INTO services (name, duration, active) "
            "VALUES ('A', 30, true), ('B', 60, false)",
        )

    def test_adapt_insert_boolean_values_conserva_clausulas_posteriores(self):
        """Lo que sigue al VALUES (ON CONFLICT, RETURNING) queda intacto."""
        sql = (
            "INSERT INTO loyalty_settings (business_id, enabled) VALUES (1, 1), (2, 0) "
            "ON CONFLICT (business_id) DO UPDATE SET enabled = EXCLUDED.enabled"
        )
        result_sql, _ = _adapt_insert_boolean_values(sql)
        self.assertEqual(
            result_sql,
            "INSERT INTO loyalty_settings (business_id, enabled) VALUES (1, true), (2, false) "
            "ON CONFLICT (business_id) DO UPDATE SET enabled = EXCLUDED.enabled",
        )

    def test_adapt_insert_boolean_values_literal_con_comas_no_rompe_alineacion(self):
        """Un literal con comas y parentesis no desalinea las filas siguientes."""
        sql = (
            "INSERT INTO knowledge (label, duration, is_open) "
            "VALUES ('a, b (c)', 30, 1), ('d', 60, 0)"
        )
        result_sql, _ = _adapt_insert_boolean_values(sql)
        self.assertEqual(
            result_sql,
            "INSERT INTO knowledge (label, duration, is_open) "
            "VALUES ('a, b (c)', 30, true), ('d', 60, false)",
        )

    def test_adapt_insert_boolean_values_literal_con_comilla_duplicada(self):
        """El escape SQL de comilla ('') no cierra el literal ni desalinea."""
        sql = "INSERT INTO knowledge (label, is_open) VALUES ('O''Brien', 1), ('x', 0)"
        result_sql, _ = _adapt_insert_boolean_values(sql)
        self.assertEqual(
            result_sql,
            "INSERT INTO knowledge (label, is_open) VALUES ('O''Brien', true), ('x', false)",
        )

    def test_adapt_insert_boolean_values_aridad_inconsistente_no_se_toca(self):
        """Una fila con aridad distinta a la lista de columnas no se reinterpreta."""
        sql = "INSERT INTO knowledge (label, is_open) VALUES ('a', 0), ('b')"
        result_sql, result_params = _adapt_insert_boolean_values(sql)
        self.assertEqual(result_sql, sql)
        self.assertIsNone(result_params)

    def test_adapt_update_boolean_params_true(self):
        """Convierte int 1 en param de columna BOOLEAN a True en UPDATE SET."""
        sql = "UPDATE businesses SET active = ?, name = ? WHERE id = ?"
        params = (1, "Mi Negocio", 1)
        _, result_params = _adapt_update_boolean_params(sql, params)
        self.assertIs(result_params[0], True)
        self.assertEqual(result_params[1], "Mi Negocio")
        self.assertEqual(result_params[2], 1)

    def test_adapt_update_boolean_params_false(self):
        """Convierte int 0 en param de columna BOOLEAN a False en UPDATE SET."""
        sql = "UPDATE businesses SET active = ? WHERE id = ?"
        params = (0, 1)
        _, result_params = _adapt_update_boolean_params(sql, params)
        self.assertIs(result_params[0], False)
        self.assertEqual(result_params[1], 1)

    def test_adapt_update_boolean_params_non_boolean_not_converted(self):
        """No transforma parámetros de columnas que no son BOOLEAN."""
        sql = "UPDATE services SET price = ?, duration = ?, active = ? WHERE id = ?"
        params = (10000, 30, 1, 5)
        _, result_params = _adapt_update_boolean_params(sql, params)
        self.assertEqual(result_params[0], 10000)
        self.assertEqual(result_params[1], 30)
        self.assertIs(result_params[2], True)
        self.assertEqual(result_params[3], 5)

    def test_adapt_update_boolean_params_no_transformation_needed(self):
        """No modifica consultas o parámetros que no necesitan adaptación."""
        sql = "UPDATE services SET price = 10000, duration = 30 WHERE id = ?"
        params = (5,)
        result_sql, result_params = _adapt_update_boolean_params(sql, params)
        self.assertEqual(result_sql, sql)
        self.assertEqual(result_params, (5,))


class TestBooleanLiteralsPorContexto(unittest.TestCase):
    """El literal 0/1 de una columna BOOLEAN se adapta SEGÚN SU CONTEXTO.

    En un predicado PostgreSQL exige `IS TRUE` / `NOT col` (no existe el operador
    `boolean = integer`); en una asignación SET exige `col = TRUE` / `col = FALSE`
    (`SET col IS TRUE` es un error de sintaxis y `SET col = 1` no castea).

    Estos casos comparan el SQL adaptado; la ejecución real contra un servidor
    PostgreSQL la cubren los tests de `TestBooleanLiteralsContraPostgresqlReal`.
    """

    def test_set_booleano_1_se_convierte_en_asignacion_valida(self):
        sql, _ = adapt_query_for_postgres(
            "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0", (7,)
        )
        self.assertIn("SET revoked = TRUE", sql)
        self.assertNotIn("SET revoked IS TRUE", sql)
        self.assertIn("AND NOT revoked", sql)

    def test_set_booleano_0_se_convierte_en_asignacion_valida(self):
        sql, _ = adapt_query_for_postgres(
            "UPDATE weekly_schedules SET is_open = 0, morning_start = NULL WHERE business_id = ?",
            (1,),
        )
        self.assertIn("SET is_open = FALSE", sql)
        self.assertNotIn("SET NOT is_open", sql)
        self.assertNotIn("SET is_open IS TRUE", sql)

    def test_set_booleano_en_on_conflict_do_update(self):
        sql, _ = adapt_query_for_postgres(
            "INSERT INTO loyalty_settings (business_id, enabled) VALUES (?, ?) "
            "ON CONFLICT (business_id) DO UPDATE SET enabled = 1",
            (1, False),
        )
        self.assertIn("DO UPDATE SET enabled = TRUE", sql)

    def test_predicados_booleanos_siguen_usando_is_true_y_not(self):
        self.assertEqual(
            adapt_query_for_postgres("SELECT * FROM knowledge WHERE active = 1")[0],
            "SELECT * FROM knowledge WHERE active IS TRUE",
        )
        self.assertEqual(
            adapt_query_for_postgres("SELECT * FROM knowledge WHERE active = 0")[0],
            "SELECT * FROM knowledge WHERE NOT active",
        )

    def test_predicado_con_calificador_conserva_la_columna_completa(self):
        """`NOT` debe preceder a la columna completa: `a.active = 0` -> `NOT a.active`."""
        sql, _ = adapt_query_for_postgres(
            "SELECT * FROM a JOIN b ON a.id = b.user_id AND b.revoked = 1 WHERE a.active = 0"
        )
        self.assertIn("b.revoked IS TRUE", sql)
        self.assertIn("WHERE NOT a.active", sql)
        self.assertNotIn("a.NOT active", sql)

    def test_update_sin_where_tambien_adapta_el_set(self):
        self.assertEqual(
            adapt_query_for_postgres("UPDATE businesses SET active = 1")[0],
            "UPDATE businesses SET active = TRUE",
        )

    def test_columnas_no_booleanas_no_se_adaptan(self):
        sql, _ = adapt_query_for_postgres("UPDATE services SET price = 0, duration = 0 WHERE id = ?")
        self.assertIn("SET price = 0, duration = 0", sql)
        self.assertNotIn("TRUE", sql)
        self.assertNotIn("NOT", sql)

    def test_select_con_set_en_literal_no_se_trata_como_clausula_set(self):
        sql, _ = adapt_query_for_postgres("SELECT 'SET' FROM sessions WHERE revoked = 0")
        self.assertIn("WHERE NOT revoked", sql)
        self.assertIn("'SET'", sql)


class TestBooleanLiteralsIgnoranTextos(unittest.TestCase):
    """PG-002: un literal de texto NUNCA es una comparación booleana.

    Las tres adaptaciones booleanas buscan con regex `col = 1` / `col = 0`. Eso es
    un comparador de igualdad entre una columna y el entero 1; dentro de un literal
    hay texto, y reescribirlo produce SQL invalido (`'active IS TRUE'` no es un
    valor) o, peor, guarda un dato distinto al que el usuario escribio.

    Se cubre tambien el escape por comilla duplicada (`'O''Brien'`), que es
    justamente lo que rompe un escaner que abra con `'` y cierre con el siguiente.
    """

    def test_literal_con_comparacion_booleana_no_se_adapta(self):
        self.assertEqual(
            adapt_query_for_postgres("SELECT * FROM knowledge WHERE question = 'active = 1'")[0],
            "SELECT * FROM knowledge WHERE question = 'active = 1'",
        )
        self.assertEqual(
            adapt_query_for_postgres("SELECT * FROM knowledge WHERE question = 'is_open = 0'")[0],
            "SELECT * FROM knowledge WHERE question = 'is_open = 0'",
        )

    def test_literal_con_comas_y_parentesis_no_se_adapta(self):
        self.assertEqual(
            adapt_query_for_postgres(
                "UPDATE t SET nota = 'not (active = 1), ni is_open = 0' WHERE id = ?", (1,)
            )[0],
            "UPDATE t SET nota = 'not (active = 1), ni is_open = 0' WHERE id = %s",
        )

    def test_literal_con_comilla_duplicada_no_se_adapta(self):
        sql, _ = adapt_query_for_postgres(
            "UPDATE t SET nota = 'O''Brien, active = 0', revoked = 1 WHERE id = ?", (1,)
        )
        self.assertIn("'O''Brien, active = 0'", sql)
        self.assertIn("revoked = TRUE", sql)

    def test_un_set_no_se_trunca_por_un_where_dentro_del_literal(self):
        """El `WHERE` de un literal no es el WHERE de la sentencia: el SET sigue entero."""
        self.assertEqual(
            adapt_query_for_postgres("UPDATE t SET nota = 'where active = 1', revoked = 1")[0],
            "UPDATE t SET nota = 'where active = 1', revoked = TRUE",
        )

    def test_comentarios_no_se_adaptan(self):
        sql, _ = adapt_query_for_postgres("-- active = 1\nSELECT 1 /* is_open = 0 */")
        self.assertIn("-- active = 1", sql)
        self.assertIn("/* is_open = 0 */", sql)
        self.assertNotIn("IS TRUE", sql)

    def test_cadena_con_dolares_no_se_adapta(self):
        sql, _ = adapt_query_for_postgres("SELECT $$active = 1$$ AS etiqueta")
        self.assertIn("$$active = 1$$", sql)
        self.assertNotIn("IS TRUE", sql)

    def test_insert_conserva_el_literal_texto_y_adapta_la_columna_booleana(self):
        sql, params = _adapt_insert_boolean_values(
            "INSERT INTO t (nota, revoked) VALUES ('active = 1', 1), ('x, y', 0)"
        )
        self.assertEqual(
            sql, "INSERT INTO t (nota, revoked) VALUES ('active = 1', true), ('x, y', false)"
        )
        self.assertIsNone(params)

    def test_params_de_update_no_se_desalinean_por_un_literal(self):
        """Un literal con `col = 1` no puede desplazar el índice del parámetro booleano.

        Antes el literal contaba como asignación, así que `revoked = ?` se
        conviene en el índice equivocado y el parámetro que se casteaba a boolean
        era el del `WHERE`.
        """
        sql, params = _adapt_update_boolean_params(
            "UPDATE t SET nota = 'active = 1', revoked = ? WHERE id = ?", ("texto", 1, 7)
        )
        self.assertEqual(sql, "UPDATE t SET nota = 'active = 1', revoked = ? WHERE id = ?")
        self.assertEqual(params, ("texto", True, 7))

    def test_los_booleanos_reales_siguen_adaptandose(self):
        """El filtro por literales no puede relajar la adaptación de código real."""
        self.assertEqual(
            adapt_query_for_postgres("UPDATE t SET revoked = 1 WHERE active = 0")[0],
            "UPDATE t SET revoked = TRUE WHERE NOT active",
        )
        self.assertEqual(
            adapt_query_for_postgres("SELECT * FROM t WHERE b.enabled != 1 AND s.revoked <> 0")[0],
            "SELECT * FROM t WHERE NOT b.enabled AND s.revoked IS TRUE",
        )


class TestBooleanLiteralsContraPostgresqlReal(unittest.TestCase):
    """Ejecuta los literales booleanos contra un PostgreSQL REAL.

    Un cursor MagicMock acepta cualquier cadena, así que el SQL inválido
    (`SET revoked IS TRUE`) nunca se detecta en los tests unitarios del adapter:
    solo el servidor lo rechaza. Estos tests envían la sentencia completa a través
    del mismo camino que usa producción (adaptación + PgConnectionProxy).
    """

    def setUp(self):
        self.connection = get_connection()
        self.user_id = create_user_scoped(
            f"bool-{uuid.uuid4().hex[:10]}@test.com", "hash-de-prueba", active=True
        )
        self.assertIsNotNone(self.user_id)

    def tearDown(self):
        try:
            self.connection.execute("DELETE FROM sessions WHERE user_id = ?", (self.user_id,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
        finally:
            self.connection.close()

    def _new_session(self, revoked=False):
        """Crea una sesión y devuelve (id, revoked) como quedaron en la base."""
        cursor = self.connection.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at, revoked) VALUES (?, ?, ?, ?)",
            (self.user_id, uuid.uuid4().hex, "2099-01-01 00:00:00", 1 if revoked else 0),
        )
        self.connection.commit()
        session_id = cursor.lastrowid
        row = self.connection.execute(
            "SELECT revoked FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return session_id, row["revoked"]

    def test_update_set_booleano_1_con_where_booleano_0(self):
        """UPDATE ... SET revoked = 1 WHERE ... revoked = 0 se ejecuta y revoca."""
        session_id, revoked = self._new_session()
        self.assertIs(revoked, False)

        cursor = self.connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0", (self.user_id,)
        )
        self.connection.commit()

        self.assertEqual(cursor.rowcount, 1)
        row = self.connection.execute(
            "SELECT revoked FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        self.assertIs(row["revoked"], True)

        # Reejecutar no vuelve a afectar la fila: el predicado ya es falso.
        cursor = self.connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0", (self.user_id,)
        )
        self.connection.commit()
        self.assertEqual(cursor.rowcount, 0)

    def test_update_set_booleano_0_con_where_booleano_1(self):
        """UPDATE ... SET revoked = 0 WHERE ... revoked = 1 se ejecuta y revierte."""
        session_id, _ = self._new_session(revoked=True)
        row = self.connection.execute(
            "SELECT revoked FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        self.assertIs(row["revoked"], True)

        cursor = self.connection.execute(
            "UPDATE sessions SET revoked = 0 WHERE user_id = ? AND revoked = 1", (self.user_id,)
        )
        self.connection.commit()

        self.assertEqual(cursor.rowcount, 1)
        row = self.connection.execute(
            "SELECT revoked FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        self.assertIs(row["revoked"], False)

    def test_revocar_todas_las_sesiones_usa_la_api_de_produccion(self):
        """`revoke_all_sessions_scoped` es la ruta de logout: debe funcionar en PG."""
        self._new_session()
        self._new_session()
        revoke_all_sessions_scoped(self.user_id)

        rows = self.connection.execute(
            "SELECT revoked FROM sessions WHERE user_id = ?", (self.user_id,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIs(row["revoked"], True)

    def test_predicado_booleano_en_select_contra_postgresql(self):
        """El mismo predicado `= 1` / `= 0` funciona como SELECT."""
        self._new_session()
        cursor = self.connection.execute(
            "SELECT id FROM sessions WHERE user_id = ? AND revoked = 0", (self.user_id,)
        )
        self.assertEqual(len(cursor.fetchall()), 1)
        cursor = self.connection.execute(
            "SELECT id FROM sessions WHERE user_id = ? AND revoked = 1", (self.user_id,)
        )
        self.assertEqual(len(cursor.fetchall()), 0)

    def _revoked_por_token(self):
        rows = self.connection.execute(
            "SELECT token_hash, revoked FROM sessions WHERE user_id = ?", (self.user_id,)
        ).fetchall()
        return {row["token_hash"]: row["revoked"] for row in rows}

    def test_insert_multi_fila_booleano_contra_postgresql(self):
        """Un multi-fila con literales 0/1 inserta TODAS las filas, no solo la primera.

        Antes del fix solo se adaptava el primer tuple y PostgreSQL rechazaba la
        sentencia con `column "revoked" is of type boolean but expression is of
        type integer` al llegar a la fila 2.
        """
        tokens = [uuid.uuid4().hex for _ in range(3)]
        self.connection.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at, revoked) "
            "VALUES (?, ?, ?, 1), (?, ?, ?, 0), (?, ?, ?, 1)",
            (
                self.user_id, tokens[0], "2099-01-01 00:00:00",
                self.user_id, tokens[1], "2099-01-01 00:00:00",
                self.user_id, tokens[2], "2099-01-01 00:00:00",
            ),
        )
        self.connection.commit()

        por_token = self._revoked_por_token()
        self.assertEqual(len(por_token), 3)
        self.assertIs(por_token[tokens[0]], True)
        self.assertIs(por_token[tokens[1]], False)
        self.assertIs(por_token[tokens[2]], True)

    def test_insert_multi_fila_placeholders_contra_postgresql(self):
        """Los params de un multi-fila no se desalinean: el 0/1 de cada fila castea a BOOLEAN."""
        tokens = [uuid.uuid4().hex for _ in range(3)]
        self.connection.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at, revoked) "
            "VALUES (?, ?, ?, ?), (?, ?, ?, ?), (?, ?, ?, ?)",
            (
                self.user_id, tokens[0], "2099-01-01 00:00:00", 1,
                self.user_id, tokens[1], "2099-01-01 00:00:00", 0,
                self.user_id, tokens[2], "2099-01-01 00:00:00", 1,
            ),
        )
        self.connection.commit()

        por_token = self._revoked_por_token()
        self.assertEqual(len(por_token), 3)
        self.assertIs(por_token[tokens[0]], True)
        self.assertIs(por_token[tokens[1]], False)
        self.assertIs(por_token[tokens[2]], True)


if __name__ == "__main__":
    unittest.main()
