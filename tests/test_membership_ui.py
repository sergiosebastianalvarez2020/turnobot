"""Pruebas de la UI real de gestión de memberships (templates/usuarios.html).

Verifica visibilidad de la sección según rol, CSRF en formularios, ausencia
de role_id/business_id como campos, y que las rutas /admin y /b/<slug>/admin
funcionan con el lenguaje visual del panel.
"""

import re
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection


class MembershipUIBase(unittest.TestCase):
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


class TestVisibilidadSeccion(MembershipUIBase):

    def test_owner_ve_la_seccion_usuarios(self):
        self._make_user("o@test.com", "secreta", 1, "owner")
        self._make_user("m1@test.com", "secreta", 1, "staff")
        self._login("", "o@test.com", "secreta")

        page = self.client.get("/admin/usuarios")
        self.assertEqual(page.status_code, 200)
        # título, formulario invitar y tabla de miembros
        self.assertIn("Invitar usuario", page.text)
        self.assertIn("m1@test.com", page.text)
        self.assertIn("Guardar", page.text)
        self.assertIn("Revocar", page.text)

    def test_admin_no_recibe_controles_de_gestion(self):
        self._make_user("a@test.com", "secreta", 1, "admin")
        self._login("", "a@test.com", "secreta")
        # Admin no puede acceder al listado de usuarios (owner-only).
        resp = self.client.get("/admin/usuarios")
        self.assertEqual(resp.status_code, 302)
        # En el panel admin no aparece el enlace de Usuarios.
        panel = self.client.get("/admin")
        self.assertEqual(panel.status_code, 200)
        self.assertNotIn("/admin/usuarios", panel.text)

    def test_staff_no_recibe_controles(self):
        self._make_user("s@test.com", "secreta", 1, "staff")
        self._login("", "s@test.com", "secreta")
        resp = self.client.get("/admin/usuarios")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))

    def test_customer_no_recibe_controles(self):
        self._make_user("c@test.com", "secreta", 1, "customer")
        resp = self.client.post(
            "/admin/usuarios/invitar",
            data={"csrf_token": "x", "email": "z@test.com", "role_name": "staff"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))


class TestFormulariosUI(MembershipUIBase):

    def setUp(self):
        super().setUp()
        self._make_user("o@test.com", "secreta", 1, "owner")
        self._make_user("m1@test.com", "secreta", 1, "staff")
        self._login("", "o@test.com", "secreta")
        self.page = self.client.get("/admin/usuarios")
        self.text = self.page.text

    def test_formulario_invitacion_contiene_csrf(self):
        # El form de invitación (action .../invitar) incluye csrf_token.
        invite_form = re.search(
            r'action="/admin/usuarios/invitar".*?</form>', self.text, re.DOTALL
        )
        self.assertIsNotNone(invite_form)
        self.assertIn('name="csrf_token"', invite_form.group(0))

    def test_formulario_cambio_rol_contiene_csrf(self):
        change_form = re.search(
            r'action="/admin/usuarios/\d+/rol".*?</form>', self.text, re.DOTALL
        )
        self.assertIsNotNone(change_form)
        self.assertIn('name="csrf_token"', change_form.group(0))

    def test_formulario_revocacion_contiene_csrf(self):
        revoke_form = re.search(
            r'action="/admin/usuarios/\d+/revocar".*?</form>', self.text, re.DOTALL
        )
        self.assertIsNotNone(revoke_form)
        self.assertIn('name="csrf_token"', revoke_form.group(0))

    def test_no_aparece_role_id_como_campo(self):
        self.assertNotIn('name="role_id"', self.text)

    def test_no_aparece_business_id_como_campo(self):
        self.assertNotIn('name="business_id"', self.text)

    def test_select_de_rol_no_ofrece_owner(self):
        # En los selects de rol (invitación y cambio) no debe haber opción owner.
        role_selects = re.findall(r'<select name="role_name".*?</select>', self.text, re.DOTALL)
        self.assertTrue(role_selects)
        for select in role_selects:
            self.assertNotIn('value="owner"', select)
            self.assertIn('value="admin"', select)
            self.assertIn('value="staff"', select)
            self.assertIn('value="customer"', select)

    def test_owner_puede_usar_las_acciones_existentes(self):
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.text).group(1)
        resp = self.client.post(
            "/admin/usuarios/invitar",
            data={"csrf_token": token, "email": "nuevo@test.com", "role_name": "customer"},
        )
        self.assertEqual(resp.status_code, 302)
        uid = database.get_user_by_email_scoped("nuevo@test.com")
        self.assertIsNotNone(uid)


class TestRutasPrefijo(MembershipUIBase):

    def test_ui_funciona_con_ruta_b_slug(self):
        owner = self._make_user("o@test.com", "secreta", 2, "owner")
        self._make_user("m1@test.com", "secreta", 2, "staff")
        self._login("business-b", "o@test.com", "secreta")

        page = self.client.get("/b/business-b/admin/usuarios")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Invitar usuario", page.text)
        self.assertIn("m1@test.com", page.text)
        # Los formularios apuntan al prefijo /b/business-b/admin/usuarios/...
        self.assertIn('action="/b/business-b/admin/usuarios/invitar"', page.text)
        # Al menos una acción de cambio de rol y una de revocación (no-propia).
        self.assertTrue(
            re.search(r'action="/b/business-b/admin/usuarios/\d+/rol"', page.text)
        )
        self.assertTrue(
            re.search(r'action="/b/business-b/admin/usuarios/\d+/revocar"', page.text)
        )

    def test_nav_usuarios_en_panel_owner(self):
        self._make_user("o@test.com", "secreta", 2, "owner")
        self._login("business-b", "o@test.com", "secreta")
        panel = self.client.get("/b/business-b/admin")
        self.assertEqual(panel.status_code, 200)
        self.assertIn("/b/business-b/admin/usuarios", panel.text)


if __name__ == "__main__":
    unittest.main()
