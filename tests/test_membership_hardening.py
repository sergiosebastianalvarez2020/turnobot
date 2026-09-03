"""Caso 14, 15, 16 y 19 — Cierre de huecos de seguridad de memberships (HTTP).

Siguiendo el principio de autorización:

    sesión -> user_id autenticado
    user_id + tenant actual -> membresía real
    membresía real -> rol real
    rol real -> autorización

El tenant se resuelve siempre desde el slug de la URL / contexto (nunca de
datos del cliente). La política owner-only vive en services.memberships y se
revalida en cada request contra business_users (sin caché).
"""

import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection


def _expires_iso(delta_seconds):
    return (
        datetime.now(tz=timezone.utc) + timedelta(seconds=delta_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")


class HardeningBase(unittest.TestCase):
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

    @staticmethod
    def _login_csrf(client, slug):
        page = client.get(f"/b/{slug}/login" if slug else "/login")
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def _login(self, client, slug, email, password):
        token = self._login_csrf(client, slug)
        return client.post(
            self._login_url(slug),
            data={"email": email, "password": password, "csrf_token": token},
        )

    def _user_csrf(self, slug, url=None):
        url = url or (f"/b/{slug}/admin/usuarios" if slug else "/admin/usuarios")
        page = self._client.get(url)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        if match:
            return match.group(1)
        admin_url = f"/b/{slug}/admin" if slug else "/admin"
        admin_page = self._client.get(admin_url)
        return re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text).group(1)

    @staticmethod
    def _role_id(name):
        return database.get_role_id_scoped(name)

    @staticmethod
    def _role_of(user_id, business_id):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT role_id FROM business_users WHERE user_id = ? AND business_id = ?",
                (user_id, business_id),
            ).fetchone()
            return row["role_id"] if row else None
        finally:
            c.close()

    @staticmethod
    def _membership_exists(user_id, business_id):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT 1 FROM business_users WHERE user_id = ? AND business_id = ?",
                (user_id, business_id),
            ).fetchone()
            return row is not None
        finally:
            c.close()

    @staticmethod
    def _establish_session(client, user_id, token):
        """Crea una sesión persistente válida y la inyecta en el cliente."""
        database.create_session_scoped(
            user_id, application._hash_session_token(token), _expires_iso(3600)
        )
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["session_token"] = token
            sess["csrf_token"] = "x"


class TestCaso14DosOwners(HardeningBase):

    def setUp(self):
        super().setUp()
        self._client = application.app.test_client()
        self.owner_a = self._make_user("a@test.com", "secreta", 1, "owner")
        self.owner_b = self._make_user("b@test.com", "secreta", 1, "owner")
        self._login(self._client, "", "a@test.com", "secreta")
        self.token = self._user_csrf("")

    def test_owner_A_puede_degradar_a_owner_B(self):
        # owner B -> customer
        resp = self._client.post(
            f"/admin/usuarios/{self.owner_b}/rol",
            data={"csrf_token": self.token, "role_name": "customer"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._role_of(self.owner_b, 1), self._role_id("customer"))
        # A sigue siendo owner
        self.assertEqual(self._role_of(self.owner_a, 1), self._role_id("owner"))

    def test_owner_A_puede_revocar_a_owner_B(self):
        self._client.post(
            f"/admin/usuarios/{self.owner_b}/rol",
            data={"csrf_token": self.token, "role_name": "admin"},
        )
        resp = self._client.post(
            f"/admin/usuarios/{self.owner_b}/revocar",
            data={"csrf_token": self.token},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self._membership_exists(self.owner_b, 1))
        self.assertTrue(self._membership_exists(self.owner_a, 1))
        self.assertEqual(self._role_of(self.owner_a, 1), self._role_id("owner"))

    def test_no_se_permite_dejar_el_negocio_sin_owners(self):
        # Con los dos owners presentes, A SÍ puede revocar a B...
        resp = self._client.post(
            f"/admin/usuarios/{self.owner_b}/revocar",
            data={"csrf_token": self.token},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self._membership_exists(self.owner_b, 1))
        # ...pero al quedar A como ÚNICO owner, ya no puede revocarse ni
        # degradarse: el negocio no puede quedar sin owners.
        resp = self._client.post(
            f"/admin/usuarios/{self.owner_a}/revocar",
            data={"csrf_token": self.token},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self._membership_exists(self.owner_a, 1))

        resp = self._client.post(
            f"/admin/usuarios/{self.owner_a}/rol",
            data={"csrf_token": self.token, "role_name": "staff"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._role_of(self.owner_a, 1), self._role_id("owner"))


class TestCaso15SesionTrasRevocacion(HardeningBase):

    def test_peticion_protegida_denegada_con_sesion_tras_revocacion(self):
        # owner A revoca la membresía de B (admin) que tiene una sesión válida.
        self._client = application.app.test_client()
        owner_a = self._make_user("a@test.com", "secreta", 1, "owner")
        b = self._make_user("b@test.com", "secreta", 1, "admin")

        # B ya tiene una sesión válida establecida (simula navegador ya logueado).
        client_b = application.app.test_client()
        token_b = "sesion-valida-B"
        self._establish_session(client_b, b, token_b)
        # Antes de revocar, B accede al panel.
        self.assertEqual(client_b.get("/admin").status_code, 200)

        # A revoca la membresía de B.
        self._login(self._client, "", "a@test.com", "secreta")
        token_a = self._user_csrf("")
        resp = self._client.post(
            f"/admin/usuarios/{b}/revocar", data={"csrf_token": token_a}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self._membership_exists(b, 1))

        # B intenta una petición protegida con su sesión existente: denegada.
        resp = client_b.get("/admin")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))

        # También la página de usuarios queda denegada.
        resp = client_b.get("/admin/usuarios")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))


class TestCaso16RolReflejadoEnSiguienteAuth(HardeningBase):

    def test_transicion_admin_a_staff_deniega_y_vuelta_admin_accede(self):
        # owner A controla el negocio; B es admin.
        self._client = application.app.test_client()
        owner_a = self._make_user("a@test.com", "secreta", 1, "owner")
        b = self._make_user("b@test.com", "secreta", 1, "admin")

        # B inicia sesión normalmente (rol admin).
        client_b = application.app.test_client()
        self._login(client_b, "", "b@test.com", "secreta")
        # Con rol admin accede al panel.
        self.assertEqual(client_b.get("/admin").status_code, 200)

        # A degrada a B a staff.
        self._login(self._client, "", "a@test.com", "secreta")
        token_a = self._user_csrf("")
        resp = self._client.post(
            f"/admin/usuarios/{b}/rol",
            data={"csrf_token": token_a, "role_name": "staff"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._role_of(b, 1), self._role_id("staff"))

        # En la siguiente petición de B ya no se conserva el rol admin.
        resp = client_b.get("/admin")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))
        resp = client_b.get("/admin/usuarios")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.headers.get("Location", ""))

        # A vuelve a promocionar a B a admin: al reingresar, el nuevo rol
        # (leído de memberships) vuelve a otorgar acceso.
        self._client.post(
            f"/admin/usuarios/{b}/rol",
            data={"csrf_token": token_a, "role_name": "admin"},
        )
        self.assertEqual(self._role_of(b, 1), self._role_id("admin"))
        # El client_b perdió su sesión al ser denegado; debe re-loguearse.
        client_b = application.app.test_client()
        self._login(client_b, "", "b@test.com", "secreta")
        self.assertEqual(client_b.get("/admin").status_code, 200)

    def test_transicion_admin_a_customer_deniega(self):
        self._client = application.app.test_client()
        owner_a = self._make_user("a@test.com", "secreta", 1, "owner")
        b = self._make_user("b@test.com", "secreta", 1, "admin")

        self._login(self._client, "", "a@test.com", "secreta")
        token_a = self._user_csrf("")
        self._client.post(
            f"/admin/usuarios/{b}/rol",
            data={"csrf_token": token_a, "role_name": "customer"},
        )
        self.assertEqual(self._role_of(b, 1), self._role_id("customer"))

        client_b = application.app.test_client()
        # Con rol customer no puede iniciar sesión administrativa.
        resp = self._login(client_b, "", "b@test.com", "secreta")
        self.assertEqual(resp.status_code, 200)
        admin = client_b.get("/admin")
        self.assertIn("login", admin.headers.get("Location", ""))


class TestCaso19NoSePuedeSaltarLaPolitica(HardeningBase):

    def setUp(self):
        super().setUp()
        self._client = application.app.test_client()
        self.owner_a = self._make_user("a@test.com", "secreta", 1, "owner")

    def test_role_id_no_permite_escalar(self):
        # M es staff. Se intenta forzar owner mediante role_id del dueño.
        m = self._make_user("m@test.com", "secreta", 1, "staff")
        owner_role_id = self._role_id("owner")
        self._login(self._client, "", "a@test.com", "secreta")
        token = self._user_csrf("")
        self._client.post(
            f"/admin/usuarios/{m}/rol",
            data={
                "csrf_token": token,
                "role_name": "staff",
                "role_id": str(owner_role_id),
            },
        )
        # policy_role: role_id enviado por el cliente se ignora.
        self.assertEqual(self._role_of(m, 1), self._role_id("staff"))
        # Tampoco role_name=owner es aceptado aunque se mande role_id owner.
        self._client.post(
            f"/admin/usuarios/{m}/rol",
            data={
                "csrf_token": token,
                "role_name": "owner",
                "role_id": str(owner_role_id),
            },
        )
        self.assertEqual(self._role_of(m, 1), self._role_id("staff"))

    def test_owner_id_y_session_id_no_influyen(self):
        m = self._make_user("m@test.com", "secreta", 1, "customer")
        self._login(self._client, "", "a@test.com", "secreta")
        token = self._user_csrf("")
        self._client.post(
            f"/admin/usuarios/{m}/rol",
            data={
                "csrf_token": token,
                "role_name": "customer",
                "owner_id": str(self.owner_a),
                "session_id": "abc123",
            },
        )
        # No hay promoción: role_name customer.
        self.assertEqual(self._role_of(m, 1), self._role_id("customer"))

    def test_business_id_inyectado_no_permite_cambiar_tenant(self):
        # M es miembro de A (staff) y de B (admin). Owner A intenta operar
        # business_id=2; la operación se resuelve en el tenant de la URL (A).
        m = self._make_user("m@test.com", "secreta", 1, "staff")
        database.create_membership_scoped(m, 2, "admin")
        self._login(self._client, "", "a@test.com", "secreta")
        token = self._user_csrf("")

        # Intento: cambiar rol con business_id=2 (debería afectar solo a A).
        self._client.post(
            f"/admin/usuarios/{m}/rol",
            data={"csrf_token": token, "role_name": "customer", "business_id": "2"},
        )
        # En el tenant real (A) el rol cambió; en B no se tocó.
        self.assertEqual(self._role_of(m, 1), self._role_id("customer"))
        self.assertEqual(self._role_of(m, 2), self._role_id("admin"))
        # Owner de A NO puede operar sobre B por URL aunque lo intente.
        resp = self._client.get("/b/business-b/admin/usuarios")
        self.assertEqual(resp.status_code, 302)

    def test_invitacion_rechaza_role_owner_a_pesar_de_role_id(self):
        owner_role_id = self._role_id("owner")
        self._login(self._client, "", "a@test.com", "secreta")
        token = self._user_csrf("")
        self._client.post(
            "/admin/usuarios/invitar",
            data={
                "csrf_token": token,
                "email": "nuevo@test.com",
                "role_name": "owner",
                "role_id": str(owner_role_id),
            },
        )
        uid = database.get_user_by_email_scoped("nuevo@test.com")
        self.assertIsNotNone(uid)
        # No se genera membresía owner.
        self.assertFalse(self._membership_exists(uid["id"], 1))


if __name__ == "__main__":
    unittest.main()
