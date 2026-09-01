import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as application
import database.database as database


class BaseIsolationTest(unittest.TestCase):
    """Base con Business A (id=1) y Business B (id=2) en base temporal."""

    UNIQUE_BUSINESS_B = False

    @classmethod
    def setUpClass(cls):
        cls._original_database_path = database.DATABASE_PATH

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self._execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )

        self.client = application.app.test_client()
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def login(self, business_id):
        business = {
            1: {"id": 1, "name": "El Corte", "slug": "el-corte"},
            2: {"id": 2, "name": "Business B", "slug": "business-b"},
        }[business_id]
        login_page = self.client.get("/login")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', login_page.text
        ).group(1)
        self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": token},
        )
        with patch.object(application, "resolve_business", return_value=business):
            with application.app.test_request_context("/"):
                application.load_current_business()
                self.csrf_token = token

    @staticmethod
    def _query(sql, params=None):
        connection = database.get_connection()
        try:
            return connection.execute(sql, params or ()).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _execute(sql, params=None):
        connection = database.get_connection()
        try:
            connection.execute(sql, params or ())
            connection.commit()
        finally:
            connection.close()


# ============================================================
# READ ISOLATION
# ============================================================

class TestReadIsolation(BaseIsolationTest):

    def test_business_a_solo_ve_sus_servicios(self):
        database.create_service_scoped(2, "Solo B", 9999, 45)
        nombres_a = {row["name"] for row in database.get_all_services_scoped(1)}
        self.assertIn("Corte", nombres_a)
        self.assertNotIn("Solo B", nombres_a)

    def test_business_b_solo_ve_sus_servicios(self):
        database.create_service_scoped(1, "Solo A", 9999, 45)
        nombres_b = {row["name"] for row in database.get_all_services_scoped(2)}
        self.assertEqual(nombres_b, set())
        activos_b = database.get_active_services_scoped(2)
        self.assertEqual(activos_b, [])


# ============================================================
# UPDATE ISOLATION
# ============================================================

class TestUpdateIsolation(BaseIsolationTest):

    def test_a_no_puede_modificar_servicio_de_b(self):
        servicio_b_id = database.create_service_scoped(2, "Servicio B", 9999, 45)
        actualizado = database.update_service_scoped(
            servicio_b_id, 1, "Modificado por A", 1111, 30, True
        )
        self.assertFalse(actualizado)
        fila = self._query(
            "SELECT name, business_id FROM services WHERE id = ?",
            (servicio_b_id,),
        )[0]
        self.assertEqual(fila["name"], "Servicio B")
        self.assertEqual(fila["business_id"], 2)

    def test_b_no_puede_modificar_servicio_de_a(self):
        servicio_a_id = database.get_all_services_scoped(1)[0]["id"]
        actualizado = database.update_service_scoped(
            servicio_a_id, 2, "Modificado por B", 1111, 30, True
        )
        self.assertFalse(actualizado)
        fila = self._query(
            "SELECT name, business_id FROM services WHERE id = ?",
            (servicio_a_id,),
        )[0]
        self.assertEqual(fila["name"], "Corte")
        self.assertEqual(fila["business_id"], 1)


# ============================================================
# TOGGLE (ACTIVAR/DESACTIVAR) ISOLATION
# ============================================================

class TestToggleIsolation(BaseIsolationTest):

    def test_a_no_puede_activar_desactivar_servicio_de_b(self):
        servicio_b_id = database.create_service_scoped(2, "Servicio B", 9999, 45)
        actualizado = database.update_service_scoped(
            servicio_b_id, 1, "Servicio B", 9999, 45, False
        )
        self.assertFalse(actualizado)
        fila = self._query(
            "SELECT active FROM services WHERE id = ?",
            (servicio_b_id,),
        )[0]
        self.assertEqual(fila["active"], 1)

    def test_b_no_puede_activar_desactivar_servicio_de_a(self):
        servicio_a = database.get_all_services_scoped(1)[0]
        servicios_a_ids = {row["id"] for row in database.get_all_services_scoped(1)}
        # B usa su scoped: no debe encontrar el servicio de A
        self.assertNotIn(servicio_a["id"], {
            row["id"] for row in database.get_all_services_scoped(2)
        })
        actualizado = database.update_service_scoped(
            servicio_a["id"], 2, servicio_a["name"],
            servicio_a["price"], servicio_a["duration"], False,
        )
        self.assertFalse(actualizado)
        fila = self._query(
            "SELECT active FROM services WHERE id = ?",
            (servicio_a["id"],),
        )[0]
        self.assertEqual(fila["active"], 1)


# ============================================================
# CREATE ASSOCIATION
# ============================================================

class TestCreateAssociation(BaseIsolationTest):

    def test_servicio_creado_por_a_queda_asociado_a_a(self):
        database.create_service_scoped(1, "Corte Premium", 15000, 40)
        fila = self._query(
            "SELECT name, business_id FROM services WHERE name = 'Corte Premium'"
        )[0]
        self.assertEqual(fila["business_id"], 1)

    def test_servicio_creado_por_b_queda_asociado_a_b(self):
        database.create_service_scoped(2, "Servicio B", 12345, 50)
        fila = self._query(
            "SELECT name, business_id FROM services WHERE name = 'Servicio B'"
        )[0]
        self.assertEqual(fila["business_id"], 2)


# ============================================================
# CLIENT-INJECTED business_id CANNOT ESCAPE CONTEXT
# ============================================================

class TestNoClientEscapesContext(BaseIsolationTest):

    def test_business_id_inyectado_por_cliente_no_escapa_el_contexto(self):
        # El admin actúa como Business A. Aunque el request intente
        # inyectar business_id por form, el backend usa get_current_business_id().
        self.login(1)
        servicio_original = database.get_all_services_scoped(1)[0]

        # Intentar actualizar el servicio de A alegando business_id=2
        response = self.client.post(
            "/admin/servicios/guardar",
            data={
                "csrf_token": self.csrf_token,
                "service_id": str(servicio_original["id"]),
                "name": "Hackeado",
                "price": "1",
                "duration": "1",
                "active": "1",
                "business_id": "2",
            },
        )
        self.assertEqual(response.status_code, 302)
        fila = self._query(
            "SELECT name, business_id FROM services WHERE id = ?",
            (servicio_original["id"],),
        )[0]
        self.assertEqual(fila["name"], "Hackeado")
        self.assertEqual(fila["business_id"], 1)


if __name__ == "__main__":
    unittest.main()
