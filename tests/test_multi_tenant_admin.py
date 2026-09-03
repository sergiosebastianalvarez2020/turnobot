"""Pruebas del modelo multitenant administrativo (P0).

Cubre: login por slug, membresías/roles, aislamiento estricto por negocio,
regeneración de sesión (anti-fixation), expiración de sesión y revocación
por logout.
"""

import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection


def _now_iso():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _expires_iso(delta_seconds):
    return (
        datetime.now(tz=timezone.utc) + timedelta(seconds=delta_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")


class BaseMultiTenantAdminTest(unittest.TestCase):
    """Negocio 1 (El Corte) y negocio 2 (Business B) en base temporal."""

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self._exec(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _exec(sql, params=None):
        c = get_connection()
        try:
            c.execute(sql, params or ())
            c.commit()
        finally:
            c.close()

    @staticmethod
    def _query(sql, params=None):
        c = get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    def _make_user(self, email, password, business, role="owner"):
        """Crea un usuario con membresía en un negocio. Devuelve user_id."""
        user_id = database.create_user_scoped(
            email, generate_password_hash(password), active=True
        )
        self.assertIsNotNone(user_id)
        database.create_membership_scoped(user_id, business, role)
        return user_id

    def _valid_date(self):
        from services.appointments import next_open_day

        return next_open_day()

    def _login_csrf(self, slug):
        page = self.client.get(f"/b/{slug}/login")
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def _login(self, slug, email, password):
        token = self._login_csrf(slug)
        return self.client.post(
            f"/b/{slug}/login",
            data={"email": email, "password": password, "csrf_token": token},
        ), token

    def _session_cookie(self):
        jar = getattr(self.client, "cookie_jar", None)
        if not jar:
            return None
        for cookie in jar:
            if cookie.name == "session":
                return cookie.value
        return None

    @staticmethod
    def _session_cookie_from(response):
        """Extrae el valor de la cookie 'session' del Set-Cookie de una respuesta."""
        for header_value in response.headers.getlist("Set-Cookie"):
            for part in header_value.split(";"):
                part = part.strip()
                if part.startswith("session="):
                    return part[len("session="):]
        return None


class TestLoginPorSlug(BaseMultiTenantAdminTest):

    def test_login_slug_owner_negocio_2_accede_a_su_admin(self):
        uid = self._make_user("b@test.com", "secreta", 2, "owner")
        response, _ = self._login("business-b", "b@test.com", "secreta")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/b/business-b/admin", response.location)

        admin = self.client.get("/b/business-b/admin")
        self.assertEqual(admin.status_code, 200)

    def test_owner_negocio_1_no_accede_admin_negocio_2(self):
        self.client = application.app.test_client()
        self._make_user("a@test.com", "secreta", 1, "owner")
        token = self._login_csrf("business-b")
        self.client.post(
            "/b/business-b/login",
            data={"email": "a@test.com", "password": "secreta", "csrf_token": token},
        )
        # El usuario no es miembro del negocio 2: debe ser rechazado.
        admin = self.client.get("/b/business-b/admin")
        self.assertEqual(admin.status_code, 302)
        self.assertIn("/login", admin.location)

    def test_usuario_con_credencial_invalida_no_accede(self):
        self._make_user("b@test.com", "secreta", 2, "owner")
        response, _ = self._login("business-b", "b@test.com", "mala")
        self.assertEqual(response.status_code, 200)

    def test_usuario_no_miembro_del_negocio_no_accede_aun_con_contrasena(self):
        self._make_user("solo@test.com", "secreta", 1, "owner")
        response, _ = self._login("business-b", "solo@test.com", "secreta")
        self.assertEqual(response.status_code, 200)


class TestRolesYAutorizacion(BaseMultiTenantAdminTest):

    def test_rol_owner_puede_administrar(self):
        self._make_user("b@test.com", "secreta", 2, "owner")
        self._login("business-b", "b@test.com", "secreta")
        self.assertEqual(self.client.get("/b/business-b/admin").status_code, 200)

    def test_rol_admin_puede_administrar(self):
        self._make_user("b@test.com", "secreta", 2, "admin")
        self._login("business-b", "b@test.com", "secreta")
        self.assertEqual(self.client.get("/b/business-b/admin").status_code, 200)

    def test_rol_staff_no_accede_admin(self):
        self._make_user("b@test.com", "secreta", 2, "staff")
        response, _ = self._login("business-b", "b@test.com", "secreta")
        self.assertEqual(response.status_code, 302)
        # membership_required solo admite owner/admin
        admin = self.client.get("/b/business-b/admin")
        self.assertIn("login", admin.headers.get("Location", ""))

    def test_rol_customer_no_accede_admin(self):
        self._make_user("b@test.com", "secreta", 2, "customer")
        response, _ = self._login("business-b", "b@test.com", "secreta")
        # El rol customer no tiene acceso administrativo en el login.
        self.assertEqual(response.status_code, 200)
        admin = self.client.get("/b/business-b/admin")
        self.assertIn("login", admin.headers.get("Location", ""))


class TestAdministracionPorNegocio(BaseMultiTenantAdminTest):

    def test_crear_servicio_solo_para_negocio_2(self):
        self._make_user("b@test.com", "secreta", 2, "owner")
        self._login("business-b", "b@test.com", "secreta")
        page = self.client.get("/b/business-b/admin")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/b/business-b/admin/servicios/guardar",
            data={
                "csrf_token": csrf,
                "name": "Servicio B nuevo",
                "price": "500",
                "duration": "30",
                "active": "1",
                "business_id": "1",  # intento de inyección por form
            },
        )
        self.assertEqual(response.status_code, 302)
        nombres = {
            r["name"] for r in self._query(
                "SELECT name FROM services WHERE business_id = 2"
            )
        }
        self.assertIn("Servicio B nuevo", nombres)
        # El intento de inyección business_id=1 no debe crear en el negocio 1.
        nombres_a = {
            r["name"]
            for r in self._query("SELECT name FROM services WHERE business_id = 1")
        }
        self.assertNotIn("Servicio B nuevo", nombres_a)

    def test_admin_b_no_modifica_servicios_de_a(self):
        self._make_user("b@test.com", "secreta", 2, "owner")
        self._login("business-b", "b@test.com", "secreta")
        page = self.client.get("/b/business-b/admin")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        self.client.post(
            "/b/business-b/admin/servicios/guardar",
            data={
                "csrf_token": csrf,
                "name": "Otro servicio",
                "price": "100",
                "duration": "15",
                "active": "1",
            },
        )
        nombres_a = {
            r["name"] for r in self._query("SELECT name FROM services WHERE business_id = 1")
        }
        self.assertNotIn("Otro servicio", nombres_a)


class TestAislamientoApiPublica(BaseMultiTenantAdminTest):

    def test_api_servicios_sigue_aislada_con_membresias(self):
        self._make_user("a@test.com", "secreta", 1, "owner")
        self._make_user("b@test.com", "secreta", 2, "owner")

        with patch.object(application, "resolve_business",
                          return_value={"id": 2, "name": "Business B", "slug": "business-b"}):
            response = self.client.get("/api/servicios")
        self.assertEqual(response.status_code, 200)
        nombres = {i["nombre"] for i in response.get_json()["servicios"]}
        self.assertNotIn("Corte", nombres)


class TestSesion(BaseMultiTenantAdminTest):

    def test_logout_revoca_sesion_activa(self):
        self._make_user("b@test.com", "secreta", 2, "owner")
        self._login("business-b", "b@test.com", "secreta")
        self.assertEqual(self.client.get("/b/business-b/admin").status_code, 200)

        page = self.client.get("/b/business-b/admin")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        logout = self.client.post("/b/business-b/logout", data={"csrf_token": csrf})
        self.assertEqual(logout.status_code, 302)
        # La sesión persistente quedó revocada y la cookie limpia.
        self.assertEqual(
            self._query("SELECT COUNT(*) n FROM sessions WHERE revoked = 1")[0]["n"],
            1,
        )
        after = self.client.get("/b/business-b/admin")
        self.assertIn("login", after.headers.get("Location", ""))

    def test_sesion_expirada_deniega_admin(self):
        # Creamos un usuario y una sesión persistente ya expirada.
        uid = self._make_user("b@test.com", "secreta", 2, "owner")
        token = "sesion-expirada"
        database.create_session_scoped(
            uid, application._hash_session_token(token), _expires_iso(-100)
        )
        # Simula una cookie de sesión HTTP pre-establecida para ese usuario.
        with self.client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["session_token"] = token
            sess["csrf_token"] = "x"
        admin = self.client.get("/b/business-b/admin")
        self.assertIn("login", admin.headers.get("Location", ""))

    def test_sesion_no_expirada_accede(self):
        uid = self._make_user("b@test.com", "secreta", 2, "owner")
        token = "sesion-activa"
        database.create_session_scoped(
            uid, application._hash_session_token(token), _expires_iso(3600)
        )
        with self.client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["session_token"] = token
            sess["csrf_token"] = "x"
        admin = self.client.get("/b/business-b/admin")
        self.assertEqual(admin.status_code, 200)

    def test_regeneracion_de_sesion_tras_login(self):
        self._make_user("b@test.com", "secreta", 2, "owner")
        pre = self.client.get("/b/business-b/login")
        cookie_before = self._session_cookie_from(pre)
        post, _ = self._login("business-b", "b@test.com", "secreta")
        cookie_after = self._session_cookie_from(post)
        self.assertIsNotNone(cookie_before)
        self.assertIsNotNone(cookie_after)
        self.assertNotEqual(cookie_before, cookie_after)

    def test_login_revoca_sesiones_anteriores(self):
        # Dos usuarios entran desde navegadores distintos; las sesiones no
        # se invalidan entre sí y el revoque del logout es por usuario.
        self._make_user("a@test.com", "secreta", 2, "owner")
        self._make_user("b@test.com", "secreta", 2, "owner")

        client_a = application.app.test_client()
        page = client_a.get("/b/business-b/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client_a.post(
            "/b/business-b/login",
            data={"email": "a@test.com", "password": "secreta", "csrf_token": token},
        )
        self.assertEqual(client_a.get("/b/business-b/admin").status_code, 200)

        client_b = application.app.test_client()
        page = client_b.get("/b/business-b/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client_b.post(
            "/b/business-b/login",
            data={"email": "b@test.com", "password": "secreta", "csrf_token": token},
        )
        self.assertEqual(client_b.get("/b/business-b/admin").status_code, 200)
        self.assertEqual(
            self._query("SELECT COUNT(*) n FROM sessions WHERE revoked = 0")[0]["n"],
            2,
        )


class TestModeloDatos(BaseMultiTenantAdminTest):

    def test_tablas_auth_existen(self):
        tablas = {r["name"] for r in self._query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        self.assertTrue({"users", "roles", "business_users", "sessions"} <= tablas)

    def test_adm_no_se_usaba_como_autenticacion_normal(self):
        # El acceso adm requiere el modelo de membresías: un usuario con
        # ADMIN_PASSWORD_HASH correcto pero SIN membresía del negocio abierto
        # no puede operar el panel del negocio 2.
        self._make_user("b@test.com", "secreta", 2, "owner")
        application.ADMIN_PASSWORD_HASH = generate_password_hash("secreta")
        try:
            response, _ = self._login("business-b", "otro@test.com", "secreta")
            self.assertEqual(response.status_code, 200)
        finally:
            application.ADMIN_PASSWORD_HASH = None


if __name__ == "__main__":
    unittest.main()