import sqlite3
import unittest

from werkzeug.middleware.proxy_fix import ProxyFix

import app as application
from application.rate_limit import is_login_request_allowed


class RateLimitingTests(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()

    def tearDown(self):
        application.rate_limit_state.clear()

    def test_forwarded_headers_are_ignored_without_proxyfix(self):
        with application.app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "10.0.0.2"},
            headers={"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.8"},
        ):
            self.assertEqual(application.get_client_ip(), "10.0.0.2")

    def test_proxyfix_uses_client_ip_when_one_proxy_is_trusted(self):
        wrapped = ProxyFix(application.app.wsgi_app, x_for=1)
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "80",
            "wsgi.url_scheme": "http",
            "wsgi.version": (1, 0),
            "wsgi.input": __import__("io").BytesIO(),
            "wsgi.errors": __import__("sys").stderr,
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "REMOTE_ADDR": "10.0.0.2",
            "HTTP_X_FORWARDED_FOR": "203.0.113.9",
        }
        captured = []

        def start_response(status, headers, exc_info=None):
            captured.append(status)

        list(wrapped(environ, start_response))
        self.assertEqual(environ["REMOTE_ADDR"], "203.0.113.9")

    def test_tenant_and_endpoint_buckets_are_separate(self):
        self.assertNotEqual(
            application._rate_limit_key("api:turnos", "1.2.3.4", 1),
            application._rate_limit_key("api:turnos", "1.2.3.4", 2),
        )
        self.assertNotEqual(
            application._rate_limit_key("api:turnos", "1.2.3.4", 1, 10),
            application._rate_limit_key("api:turnos", "1.2.3.4", 1, 11),
        )

    def test_rate_limit_state_is_in_process_only(self):
        """El rate limiting usa memoria del proceso actual.

        Nota: en despliegues multi-worker o multi-proceso, cada worker
        mantendría su propio rate_limit_state, por lo que los límites
        podrían aplicarse de forma independiente por worker. Para soportar
        múltiples workers, en el futuro se requiere almacenamiento compartido
        como Redis; actualmente no se agrega porque el despliegue oficial
        de TurnoBot usa un solo proceso Waitress.
        """
        application.rate_limit_state.clear()
        self.assertFalse(application.rate_limit_state)
        application.is_chat_request_allowed("1.2.3.4", 1)
        self.assertIn(
            application._rate_limit_key("chat", "1.2.3.4", 1), application.rate_limit_state
        )

    def test_login_rate_limit_per_tenant(self):
        """Verifica que el rate limit de login distinga por business_id."""
        ip = "1.2.3.4"
        # Límite es 10 req por ventana
        limit = 10

        # Requests para business_id=1
        for i in range(limit):
            self.assertTrue(
                is_login_request_allowed(ip, business_id=1),
                f"Request {i + 1} para business_id=1 debería ser permitido",
            )
        # El siguiente debe ser bloqueado para business_id=1
        self.assertFalse(
            is_login_request_allowed(ip, business_id=1),
            "Request 11 para business_id=1 debería ser bloqueado",
        )

        # Pero para business_id=2 con la misma IP, debería seguir permitido
        for i in range(limit):
            self.assertTrue(
                is_login_request_allowed(ip, business_id=2),
                f"Request {i + 1} para business_id=2 debería ser permitido",
            )
        self.assertFalse(
            is_login_request_allowed(ip, business_id=2),
            "Request 11 para business_id=2 debería ser bloqueado",
        )

        # Y sin business_id (fallback) también separado
        for i in range(limit):
            self.assertTrue(
                is_login_request_allowed(ip, business_id=None),
                f"Request {i + 1} sin business_id debería ser permitido",
            )
        self.assertFalse(
            is_login_request_allowed(ip, business_id=None),
            "Request 11 sin business_id debería ser bloqueado",
        )


if __name__ == "__main__":
    unittest.main()


class ForeignKeyEnforcementTests(unittest.TestCase):
    """Tests que verifican que SQLite rechaza referencias inválidas
    cuando PRAGMA foreign_keys = ON (configuración de la app)."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        import database.database as database

        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        import database.database as database

        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_foreign_key_rejects_invalid_business_id(self):
        """INSERT con business_id inexistente debe fallar por FK."""
        import database.database as database

        conn = database.get_connection()
        try:
            # Intentar insertar appointment con business_id inexistente (99999)
            conn.execute(
                """
                INSERT INTO appointments
                (customer_name, phone, service, appointment_date,
                 appointment_time, appointment_end, duration, business_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Test",
                    "12345678",
                    "Corte",
                    "2025-01-01",
                    "10:00",
                    "11:00",
                    60,
                    99999,
                    "confirmed",
                ),
            )
            conn.commit()
            self.fail("Debió fallar por foreign key constraint")
        except sqlite3.IntegrityError:
            # Esperado: FK constraint falla
            pass
        finally:
            conn.close()

    def test_foreign_key_rejects_invalid_resource_id(self):
        """INSERT con resource_id inexistente debe fallar por FK."""
        import sqlite3

        import database.database as database

        # Primero crear un negocio y servicio válidos
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO services (business_id, name, price, duration, active) VALUES (1, 'Test', 1000, 30, 1)"
            )
            conn.commit()
            conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

        conn = database.get_connection()
        try:
            # Intentar insertar appointment con resource_id inexistente
            conn.execute(
                """
                INSERT INTO appointments
                (customer_name, phone, service, appointment_date,
                 appointment_time, appointment_end, duration, business_id, resource_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Test",
                    "12345678",
                    "Test",
                    "2025-01-01",
                    "10:00",
                    "10:30",
                    30,
                    1,
                    99999,
                    "confirmed",
                ),
            )
            conn.commit()
            self.fail("Debió fallar por foreign key constraint")
        except sqlite3.IntegrityError:
            # Esperado: FK constraint falla
            pass
        finally:
            conn.close()

    def test_foreign_key_cascade_delete_business(self):
        """Eliminar negocio debe fallar si tiene citas (restrict)."""
        import sqlite3

        import database.database as database

        conn = database.get_connection()
        try:
            # Crear negocio y cita
            conn.execute("INSERT INTO businesses (name, slug) VALUES ('Test', 'test')")
            conn.commit()
            biz_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            conn.execute(
                """
                INSERT INTO appointments
                (customer_name, phone, service, appointment_date,
                 appointment_time, appointment_end, duration, business_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Test",
                    "12345678",
                    "Corte",
                    "2025-01-01",
                    "10:00",
                    "11:00",
                    60,
                    biz_id,
                    "confirmed",
                ),
            )
            conn.commit()

            # Intentar borrar negocio - debe fallar por FK restrict
            conn.execute("DELETE FROM businesses WHERE id = ?", (biz_id,))
            conn.commit()
            self.fail("Debió fallar por foreign key constraint (RESTRICT)")
        except sqlite3.IntegrityError:
            # Esperado: FK constraint falla
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
