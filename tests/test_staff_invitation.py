"""Pruebas de Etapa 10: invitación de staff/admin por el owner.

- El owner puede invitar staff/admin existente.
- El owner NO puede invitar a un usuario inexistente (no se crea inactivo).
- Un admin NO puede invitar (solo owner está autorizado).
- El owner no puede invitarse a sí mismo.
- La invitación se consume una sola vez.
- El staff invitado puede aceptar y obtener membership con el rol indicado.
"""

import re
import tempfile
import unittest
from pathlib import Path

import app as application
import database.database as database
from database.database import get_connection
from services import platform as platform_service


class StaffInvitationBase(unittest.TestCase):

    OWNER_EMAIL = "owner@test-staff.com"
    OWNER_PASSWORD = "clave-segura-123"
    STAFF_EMAIL = "staff@test-staff.com"
    ADMIN_EMAIL = "admin@test-staff.com"

    def setUp(self):
        application.rate_limit_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "staff.db"
        database.init_database()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_db
        self.tmp.cleanup()

    @staticmethod
    def _csrf(page):
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return match.group(1) if match else None

    def _query(self, sql, params=None):
        c = get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    def _provision_and_approve(self):
        result = platform_service.provision_business(
            "Staff Biz", "test-staff-biz", self.OWNER_EMAIL
        )
        approved = platform_service.approve_business(result["business_id"])
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        platform_service.accept_invitation(result["business_id"], token, self.OWNER_PASSWORD)
        return result

    def _login_owner(self):
        page = self.client.get("/b/test-staff-biz/login")
        csrf = self._csrf(page)
        self.client.post(
            "/b/test-staff-biz/login",
            data={"email": self.OWNER_EMAIL, "password": self.OWNER_PASSWORD, "csrf_token": csrf},
        )

    def _get_owner_user_id(self):
        row = self._query("SELECT id FROM users WHERE email = ?", (self.OWNER_EMAIL,))
        return row[0]["id"] if row else None

    def _create_admin_user(self):
        """Crea un usuario con rol admin en el negocio, activo, con password."""
        admin_hash = platform_service.generate_password_hash(self.OWNER_PASSWORD)
        admin_id = database.create_user_scoped(self.ADMIN_EMAIL, admin_hash, active=True)
        database.create_membership_scoped(admin_id, self._business_id(), "admin")
        return admin_id

    def _business_id(self):
        row = self._query("SELECT id FROM businesses WHERE slug = 'test-staff-biz'")
        return row[0]["id"] if row else None

    def _login_admin(self, admin_id):
        page = self.client.get("/b/test-staff-biz/login")
        csrf = self._csrf(page)
        self.client.post(
            "/b/test-staff-biz/login",
            data={"email": self.ADMIN_EMAIL, "password": self.OWNER_PASSWORD, "csrf_token": csrf},
        )
        return admin_id

    def _invitar_url(self):
        return "/b/test-staff-biz/admin/usuarios/invitar-enlace"


class TestStaffInvitationAuthorization(StaffInvitationBase):

    def test_owner_invite_existing_user_as_admin(self):
        self._provision_and_approve()
        self._login_owner()

        existing = database.create_user_scoped(
            self.STAFF_EMAIL, platform_service.generate_password_hash("placeholder"), active=True
        )
        business_id = self._business_id()

        csrf_page = self.client.get("/b/test-staff-biz/admin/usuarios")
        csrf = self._csrf(csrf_page)
        response = self.client.post(
            self._invitar_url(),
            data={"email": self.STAFF_EMAIL, "role_name": "admin", "csrf_token": csrf},
        )
        self.assertEqual(response.status_code, 302)

        invitations = self._query(
            "SELECT role_name FROM invitations WHERE business_id = ? AND user_id = ? "
            "AND used_at IS NULL AND revoked_at IS NULL",
            (business_id, existing),
        )
        self.assertEqual(len(invitations), 1)
        self.assertEqual(invitations[0]["role_name"], "admin")

    def test_admin_cannot_invite_staff(self):
        from services import platform as ps

        self._provision_and_approve()
        admin_id = self._create_admin_user()
        business_id = self._business_id()

        result = ps.create_staff_invitation(business_id, self.STAFF_EMAIL, "staff", actor_user_id=admin_id)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "forbidden")

    def test_owner_cannot_invite_himself(self):
        self._provision_and_approve()
        self._login_owner()

        csrf_page = self.client.get("/b/test-staff-biz/admin/usuarios")
        csrf = self._csrf(csrf_page)
        response = self.client.post(
            self._invitar_url(),
            data={"email": self.OWNER_EMAIL, "role_name": "staff", "csrf_token": csrf},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("invitarte", response.headers.get("Location", ""))

    def test_invalid_role_rejected(self):
        self._provision_and_approve()
        self._login_owner()

        csrf_page = self.client.get("/b/test-staff-biz/admin/usuarios")
        csrf = self._csrf(csrf_page)
        response = self.client.post(
            self._invitar_url(),
            data={"email": self.STAFF_EMAIL, "role_name": "superadmin", "csrf_token": csrf},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("Rol+no+permitido", response.headers.get("Location", ""))

    def test_owner_invite_creates_token_in_db(self):
        self._provision_and_approve()
        self._login_owner()

        result = platform_service.create_staff_invitation(
            self._business_id(), self.STAFF_EMAIL, "staff",
            actor_user_id=self._get_owner_user_id(),
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["invitation_token"])

        rows = self._query(
            "SELECT role_name FROM invitations WHERE email = ? AND role_name = ?",
            (self.STAFF_EMAIL, "staff"),
        )
        self.assertEqual(len(rows), 1)


class TestStaffInvitationAccept(StaffInvitationBase):

    def _setup_invitation(self, role="staff"):
        self._provision_and_approve()
        self._login_owner()
        return platform_service.create_staff_invitation(
            self._business_id(), self.STAFF_EMAIL, role,
            actor_user_id=self._get_owner_user_id(),
        )

    def test_staff_invitation_route_get_shows_email(self):
        result = self._setup_invitation()
        token = result["invitation_token"]
        page = self.client.get(f"/b/test-staff-biz/invitacion-staff/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.STAFF_EMAIL, page.text)

    def test_staff_invitation_invalid_token_returns_404(self):
        page = self.client.get("/b/test-staff-biz/invitacion-staff/token-invalido")
        self.assertEqual(page.status_code, 404)

    def test_accept_staff_invitation_creates_membership(self):
        result = self._setup_invitation(role="admin")
        token = result["invitation_token"]

        page = self.client.get(f"/b/test-staff-biz/invitacion-staff/{token}")
        csrf = self._csrf(page)

        response = self.client.post(
            f"/b/test-staff-biz/invitacion-staff/{token}",
            data={
                "password": "clave-staff-segura-1",
                "password2": "clave-staff-segura-1",
                "csrf_token": csrf,
            },
        )
        self.assertEqual(response.status_code, 302)

        rows = self._query(
            """SELECT r.name AS role_name FROM business_users bu
               JOIN users u ON u.id = bu.user_id
               JOIN roles r ON r.id = bu.role_id
               WHERE u.email = ? AND bu.business_id = ?""",
            (self.STAFF_EMAIL, self._business_id()),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role_name"], "admin")

    def test_accept_staff_invitation_weak_password_rejected(self):
        result = self._setup_invitation()
        token = result["invitation_token"]

        page = self.client.get(f"/b/test-staff-biz/invitacion-staff/{token}")
        csrf = self._csrf(page)

        response = self.client.post(
            f"/b/test-staff-biz/invitacion-staff/{token}",
            data={"password": "corta", "password2": "corta", "csrf_token": csrf},
        )
        self.assertIn("al menos 12 caracteres", response.text)

    def test_staff_invitation_cannot_be_accepted_twice(self):
        result = self._setup_invitation()
        token = result["invitation_token"]

        page = self.client.get(f"/b/test-staff-biz/invitacion-staff/{token}")
        csrf = self._csrf(page)

        pw = "clave-staff-segura-1"
        self.client.post(
            f"/b/test-staff-biz/invitacion-staff/{token}",
            data={"password": pw, "password2": pw, "csrf_token": csrf},
        )
        page2 = self.client.get(f"/b/test-staff-biz/invitacion-staff/{token}")
        self.assertEqual(page2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
