"""Pruebas HTTP de los endpoints de gestión de memberships (PASO endpoints).

Cubre aislamiento por tenant, autorización owner-only, invitación, cambio de
rol, revocación y CSRF. Sigue los patrones de test_multi_tenant_admin.py.
"""

import re
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection


class MembershipHTTPBase(unittest.TestCase):
    """Negocio 1 y negocio 2 en base temporal."""

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

    def _make_user(self, email, password, business, role="owner"):
        user_id = database.create_user_scoped(
            email, generate_password_hash(password), active=True
        )
        self.assertIsNotNone(user_id)
        database.create_membership_scoped(user_id, business, role)
        return user_id

    def _login_url(self, slug):
        return f"/b/{slug}/login" if slug else "/login"

    def _login_csrf(self, slug):
        page = self.client.get(self._login_url(slug))
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def _login(self, slug, email, password):
        token = self._login_csrf(slug)
        return self.client.post(
            self._login_url(slug),
            data={"email": email, "password": password, "csrf_token": token},
        )

    def _page_csrf(self, url):
        page = self.client.get(url)
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1), page

    def _user_csrf(self, slug):
        """Extrae un CSRF de sesión válido para POSTs del actor.

        Para el owner se usa la página de usuarios; para roles sin acceso a
        la lista (p.ej. admin) se cae al panel admin, que también expone el
        token de sesión.
        """
        url = f"/b/{slug}/admin/usuarios" if slug else "/admin/usuarios"
        page = self.client.get(url)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        if match:
            return match.group(1)
        admin_url = f"/b/{slug}/admin" if slug else "/admin"
        admin_page = self.client.get(admin_url)
        return re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text).group(1)

    def _role_of(self, user_id, business_id):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT role_id FROM business_users WHERE user_id = ? AND business_id = ?",
                (user_id, business_id),
            ).fetchone()
            return row["role_id"] if row else None
        finally:
            c.close()

    def _membership_exists(self, user_id, business_id):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT 1 FROM business_users WHERE user_id = ? AND business_id = ?",
                (user_id, business_id),
            ).fetchone()
            return row is not None
        finally:
            c.close()


class TestTenantIsolation(MembershipHTTPBase):

    def test_owner_de_A_gestiona_miembros_de_A(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        token, page = self._page_csrf("/admin/usuarios")
        self.assertEqual(page.status_code, 200)
        self.assertIn("o@test.com", page.text)
        # Invitar un miembro nuevo a A.
        self.client.post(
            "/admin/usuarios/invitar",
            data={"csrf_token": token, "email": "nuevo@test.com", "role_name": "staff"},
        )
        nuevo = database.get_user_by_email_scoped("nuevo@test.com")
        self.assertIsNotNone(nuevo)
        self.assertEqual(
            self._role_of(nuevo["id"], 1), database.get_role_id_scoped("staff")
        )

    def test_owner_de_A_no_gestiona_miembros_de_B(self):
        owner_a = self._make_user("o@test.com", "secreta", 1, "owner")
        self._make_user("b@test.com", "secreta", 2, "owner")
        self._login("", "o@test.com", "secreta")
        # GET en el tenant B se rechaza en el gate administrativo (no es miembro).
        listing = self.client.get("/b/business-b/admin/usuarios")
        self.assertEqual(listing.status_code, 302)
        # POST de invitación a B igualmente rechazado y sin efecto.
        token = "irrelevante"
        resp = self.client.post(
            "/b/business-b/admin/usuarios/invitar",
            data={"csrf_token": token, "email": "x@test.com", "role_name": "staff"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(database.get_user_by_email_scoped("x@test.com"))

    def test_url_b_slug_usa_B_como_tenant(self):
        b_owner = self._make_user("o@test.com", "secreta", 2, "owner")
        self._make_user("b1@test.com", "secreta", 2, "staff")
        self._make_user("a1@test.com", "secreta", 1, "owner")
        self._login("business-b", "o@test.com", "secreta")
        token, page = self._page_csrf("/b/business-b/admin/usuarios")
        self.assertEqual(page.status_code, 200)
        self.assertIn("b1@test.com", page.text)
        self.assertNotIn("a1@test.com", page.text)

    def test_manipular_business_id_no_cambia_tenant_en_el_POST(self):
        # Owner de B revoca a un miembro de B inyectando business_id=1:
        # la operación sigue apuntando a B, no a A.
        owner_b = self._make_user("o@test.com", "secreta", 2, "owner")
        b_member = self._make_user("b1@test.com", "secreta", 2, "staff")
        self._login("business-b", "o@test.com", "secreta")
        token = self._user_csrf("business-b")
        self.client.post(
            f"/b/business-b/admin/usuarios/{b_member}/revocar",
            data={"csrf_token": token, "business_id": "1"},
        )
        self.assertFalse(self._membership_exists(b_member, 2))

    def test_user_id_de_otro_tenant_no_modifica_ese_usuario(self):
        owner_a = self._make_user("o@test.com", "secreta", 1, "owner")
        b_member = self._make_user("b1@test.com", "secreta", 2, "admin")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        # Owner de A intenta cambiar el rol de un miembro de B en A.
        self.client.post(
            f"/admin/usuarios/{b_member}/rol",
            data={"csrf_token": token, "role_name": "staff"},
        )
        # Sin efecto: el miembro pertenece a B y no es miembro de A.
        self.assertEqual(
            self._role_of(b_member, 2), database.get_role_id_scoped("admin")
        )

    def test_no_hay_ruta_para_seleccionar_business_id_arbitrario(self):
        # Las rutas admin/usuarios no aceptan business_id como parámetro de URL.
        owner_a = self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        self.assertEqual(self.client.get("/admin/usuarios").status_code, 200)
        self.assertEqual(self.client.get("/admin/usuarios?business_id=2").status_code, 200)
        self.assertNotIn("b@test", self.client.get("/admin/usuarios?business_id=2").text)


class TestAutorizacionHTTP(MembershipHTTPBase):

    def test_owner_puede_listar(self):
        self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        self.assertEqual(self.client.get("/admin/usuarios").status_code, 200)

    def test_admin_no_puede_mutar_memberships(self):
        admin = self._make_user("a@test.com", "secreta", 1, "admin")
        member = self._make_user("m@test.com", "secreta", 1, "staff")
        self._login("", "a@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            f"/admin/usuarios/{member}/rol",
            data={"csrf_token": token, "role_name": "customer"},
        )
        self.assertEqual(
            self._role_of(member, 1), database.get_role_id_scoped("staff")
        )

    def test_staff_no_administra(self):
        self._make_user("s@test.com", "secreta", 1, "staff")
        self._login("", "s@test.com", "secreta")
        resp = self.client.get("/admin/usuarios")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))

    def test_customer_no_administra(self):
        self._make_user("c@test.com", "secreta", 1, "customer")
        resp = self.client.post(
            "/admin/usuarios/invitar",
            data={"csrf_token": "x", "email": "z@test.com", "role_name": "staff"},
        )
        # customer no puede iniciar sesión administrativa → redirige a login.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))

    def test_owner_puede_invitar_staff_customer_admin(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        for role in ("staff", "customer", "admin"):
            self.client.post(
                "/admin/usuarios/invitar",
                data={"csrf_token": token, "email": f"{role}@test.com", "role_name": role},
            )
            uid = database.get_user_by_email_scoped(f"{role}@test.com")["id"]
            self.assertEqual(
                self._role_of(uid, 1), database.get_role_id_scoped(role)
            )

    def test_owner_no_puede_asignar_owner(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            "/admin/usuarios/invitar",
            data={"csrf_token": token, "email": "nuevo@test.com", "role_name": "owner"},
        )
        uid = database.get_user_by_email_scoped("nuevo@test.com")
        self.assertIsNotNone(uid)
        # La invitación a owner se rechaza por la política → no hay membership.
        self.assertFalse(self._membership_exists(uid["id"], 1))

    def test_admin_no_puede_asignar_owner_ni_admin(self):
        admin = self._make_user("a@test.com", "secreta", 1, "admin")
        staff = self._make_user("s@test.com", "secreta", 1, "staff")
        self._login("", "a@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            f"/admin/usuarios/{staff}/rol",
            data={"csrf_token": token, "role_name": "owner"},
        )
        self.client.post(
            f"/admin/usuarios/{staff}/rol",
            data={"csrf_token": token, "role_name": "admin"},
        )
        self.assertEqual(
            self._role_of(staff, 1), database.get_role_id_scoped("staff")
        )

    def test_owner_no_puede_revocarse(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            f"/admin/usuarios/{owner}/revocar", data={"csrf_token": token}
        )
        self.assertTrue(self._membership_exists(owner, 1))

    def test_owner_no_puede_revocar_al_ultimo_owner(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            f"/admin/usuarios/{owner}/revocar", data={"csrf_token": token}
        )
        self.assertTrue(self._membership_exists(owner, 1))

    def test_owner_puede_revocar_un_miembro_permitido(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        member = self._make_user("m@test.com", "secreta", 1, "staff")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            f"/admin/usuarios/{member}/revocar", data={"csrf_token": token}
        )
        self.assertFalse(self._membership_exists(member, 1))
        self.assertTrue(self._membership_exists(owner, 1))

    def test_owner_cambia_roles_segun_politica(self):
        owner = self._make_user("o@test.com", "secreta", 1, "owner")
        member = self._make_user("m@test.com", "secreta", 1, "staff")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(
            f"/admin/usuarios/{member}/rol",
            data={"csrf_token": token, "role_name": "admin"},
        )
        self.assertEqual(
            self._role_of(member, 1), database.get_role_id_scoped("admin")
        )
        # intento de promover a owner: sin efecto
        self.client.post(
            f"/admin/usuarios/{member}/rol",
            data={"csrf_token": token, "role_name": "owner"},
        )
        self.assertEqual(
            self._role_of(member, 1), database.get_role_id_scoped("admin")
        )


class TestCSRF(MembershipHTTPBase):

    def test_post_sin_csrf_devuelve_400(self):
        self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        resp = self.client.post(
            "/admin/usuarios/invitar",
            data={"email": "n@test.com", "role_name": "staff"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(database.get_user_by_email_scoped("n@test.com"))

    def test_post_con_csrf_valido_prosigue(self):
        self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        resp = self.client.post(
            "/admin/usuarios/invitar",
            data={"csrf_token": token, "email": "n@test.com", "role_name": "staff"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(database.get_user_by_email_scoped("n@test.com"))

    def test_get_no_muta_datos(self):
        self._make_user("o@test.com", "secreta", 1, "owner")
        self._login("", "o@test.com", "secreta")
        self.client.get("/admin/usuarios")
        self.assertEqual(
            self._query_count("SELECT COUNT(*) n FROM business_users")["n"], 1
        )

    def _query_count(self, sql):
        c = get_connection()
        try:
            return c.execute(sql).fetchone()
        finally:
            c.close()


class TestAislamientoRevocacionNoCreaHuecos(MembershipHTTPBase):

    def test_revocar_en_A_no_quita_membership_en_B(self):
        # Un usuario miembro de A y B; al revocarlo en A queda en B.
        owner_a = self._make_user("o@test.com", "secreta", 1, "owner")
        user = self._make_user("u@test.com", "secreta", 1, "staff")
        database.create_membership_scoped(user, 2, "admin")
        self._login("", "o@test.com", "secreta")
        token = self._user_csrf("")
        self.client.post(f"/admin/usuarios/{user}/revocar", data={"csrf_token": token})
        self.assertFalse(self._membership_exists(user, 1))
        self.assertTrue(self._membership_exists(user, 2))


if __name__ == "__main__":
    unittest.main()
