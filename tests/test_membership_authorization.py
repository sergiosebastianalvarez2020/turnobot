"""Pruebas de la capa de autorización de memberships (services/memberships.py).

Cubre aislamiento por negocio, jerarquía de roles y reglas sobre owners.
Ninguna operación usa business_id aportado por el cliente.
"""

import tempfile
import unittest
from pathlib import Path

from services import memberships

from werkzeug.security import generate_password_hash

import database.database as database
from database.database import get_connection


class MembershipAuthBase(unittest.TestCase):
    """Negocio 1 y negocio 2 en base temporal."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self._exec(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
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


class TestAislamiento(MembershipAuthBase):

    def test_owner_de_A_no_lista_miembros_de_B(self):
        owner_a = self._make_user("a@test.com", "secret", 1, "owner")
        self._make_user("b1@test.com", "secret", 2, "owner")

        result = memberships.list_members(owner_a, 2)
        self.assertFalse(result["success"])
        self.assertIsNone(result.get("members"))

        result_a = memberships.list_members(owner_a, 1)
        self.assertTrue(result_a["success"])
        self.assertTrue(result_a["members"])

    def test_owner_de_A_no_cambia_rol_en_B(self):
        owner_a = self._make_user("a@test.com", "secret", 1, "owner")
        b_admin = self._make_user("b1@test.com", "secret", 2, "admin")

        result = memberships.change_role(owner_a, 2, b_admin, "staff")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(b_admin, 2), memberships._role_name_to_id("admin"))

    def test_owner_de_A_no_revoca_en_B(self):
        owner_a = self._make_user("a@test.com", "secret", 1, "owner")
        b_admin = self._make_user("b1@test.com", "secret", 2, "admin")

        result = memberships.revoke_membership(owner_a, 2, b_admin)
        self.assertFalse(result["success"])
        self.assertTrue(self._membership_exists(b_admin, 2))

    def test_business_id_manipulado_no_cambia_tenant_efectivo(self):
        # El negocio operado es SIEMPRE el pasado/preseleccionado por la capa
        # superior. Un owner de A que intente operar B es rechazado y B no cambia.
        owner_a = self._make_user("a@test.com", "secret", 1, "owner")
        b_staff = self._make_user("b1@test.com", "secret", 2, "staff")

        result = memberships.revoke_membership(owner_a, 2, b_staff)
        self.assertFalse(result["success"])
        self.assertTrue(self._membership_exists(b_staff, 2))
        # La operación legítima dentro de A sí funciona.
        a_staff = self._make_user("a2@test.com", "secret", 1, "staff")
        revoked = memberships.revoke_membership(owner_a, 1, a_staff)
        self.assertTrue(revoked["success"])


class TestJerarquia(MembershipAuthBase):

    def test_owner_puede_asignar_admin_staff_customer(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        # Los usuarios destino existen globalmente (miembros de otro negocio)
        # y se agregan a este negocio mediante invite, sin duplicar usuario.
        for role in ("admin", "staff", "customer"):
            uid = self._make_user(f"{role}@test.com", "secret", 2, role)
            result = memberships.invite_member(owner, 1, uid, role)
            self.assertTrue(result["success"], role)
            self.assertEqual(self._role_of(uid, 1), memberships._role_name_to_id(role))

    def test_owner_no_puede_asignar_owner(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        user = self._make_user("u@test.com", "secret", 1, "customer")
        result = memberships.invite_member(owner, 1, user, "owner")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(user, 1), memberships._role_name_to_id("customer"))

    def test_owner_no_puede_promover_a_owner(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        staff = self._make_user("s@test.com", "secret", 1, "staff")
        result = memberships.change_role(owner, 1, staff, "owner")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(staff, 1), memberships._role_name_to_id("staff"))

    def test_admin_no_puede_asignar_owner(self):
        admin = self._make_user("a@test.com", "secret", 1, "admin")
        user = self._make_user("u@test.com", "secret", 1, "customer")
        result = memberships.invite_member(admin, 1, user, "owner")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(user, 1), memberships._role_name_to_id("customer"))

    def test_admin_no_puede_promover_admin(self):
        admin = self._make_user("a@test.com", "secret", 1, "admin")
        staff = self._make_user("s@test.com", "secret", 1, "staff")
        result = memberships.change_role(admin, 1, staff, "admin")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(staff, 1), memberships._role_name_to_id("staff"))

    def test_staff_no_administra(self):
        staff = self._make_user("s@test.com", "secret", 1, "staff")
        admin_b = self._make_user("b@test.com", "secret", 1, "admin")
        self.assertFalse(memberships.can_manage_memberships(staff, 1))
        self.assertFalse(memberships.list_members(staff, 1)["success"])
        self.assertFalse(memberships.revoke_membership(staff, 1, admin_b)["success"])
        self.assertTrue(self._membership_exists(admin_b, 1))

    def test_customer_no_administra(self):
        customer = self._make_user("c@test.com", "secret", 1, "customer")
        self.assertFalse(memberships.can_manage_memberships(customer, 1))
        self.assertFalse(memberships.list_members(customer, 1)["success"])


class TestOwners(MembershipAuthBase):

    def test_owner_no_puede_revocarse_a_si_mismo(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        result = memberships.revoke_membership(owner, 1, owner)
        self.assertFalse(result["success"])
        self.assertTrue(self._membership_exists(owner, 1))

    def test_no_se_puede_revocar_al_ultimo_owner(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        self._make_user("x@test.com", "secret", 1, "staff")
        result = memberships.revoke_membership(owner, 1, owner)
        self.assertFalse(result["success"])
        self.assertTrue(self._membership_exists(owner, 1))

    def test_no_se_puede_degradar_al_ultimo_owner(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        result = memberships.change_role(owner, 1, owner, "staff")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(owner, 1), memberships._role_name_to_id("owner"))

    def test_con_dos_owners_uno_puede_ser_revocado(self):
        owner_a = self._make_user("a@test.com", "secret", 1, "owner")
        owner_b = self._make_user("b@test.com", "secret", 1, "owner")
        result = memberships.revoke_membership(owner_a, 1, owner_b)
        self.assertTrue(result["success"])
        self.assertFalse(self._membership_exists(owner_b, 1))
        self.assertTrue(self._membership_exists(owner_a, 1))

    def test_con_dos_owners_uno_puede_ser_degradado(self):
        owner_a = self._make_user("a@test.com", "secret", 1, "owner")
        owner_b = self._make_user("b@test.com", "secret", 1, "owner")
        result = memberships.change_role(owner_a, 1, owner_b, "admin")
        self.assertTrue(result["success"])
        self.assertEqual(self._role_of(owner_b, 1), memberships._role_name_to_id("admin"))
        self.assertEqual(self._role_of(owner_a, 1), memberships._role_name_to_id("owner"))


class TestCambioDeRol(MembershipAuthBase):

    def test_cambio_valido_dentro_del_mismo_negocio(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        staff = self._make_user("s@test.com", "secret", 1, "staff")
        result = memberships.change_role(owner, 1, staff, "admin")
        self.assertTrue(result["success"])
        self.assertEqual(self._role_of(staff, 1), memberships._role_name_to_id("admin"))

    def test_usuario_inexistente_no_tiene_efecto(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        result = memberships.change_role(owner, 1, 999999, "staff")
        self.assertFalse(result["success"])

    def test_role_destino_invalido_no_tiene_efecto(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        staff = self._make_user("s@test.com", "secret", 1, "staff")
        result = memberships.change_role(owner, 1, staff, "no-existe")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(staff, 1), memberships._role_name_to_id("staff"))

    def test_cambiar_rol_en_A_no_modifica_B(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        user = self._make_user("u@test.com", "secret", 2, "admin")
        result = memberships.change_role(owner, 1, user, "staff")
        self.assertFalse(result["success"])
        self.assertEqual(self._role_of(user, 2), memberships._role_name_to_id("admin"))


class TestRevocacion(MembershipAuthBase):

    def test_owner_puede_revocar_staff_admin_customer(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        for role in ("staff", "admin", "customer"):
            uid = self._make_user(f"{role}@test.com", "secret", 1, role)
            result = memberships.revoke_membership(owner, 1, uid)
            self.assertTrue(result["success"], role)
            self.assertFalse(self._membership_exists(uid, 1))

    def test_revocar_miembro_de_otro_tenant_no_tiene_efecto(self):
        owner = self._make_user("o@test.com", "secret", 1, "owner")
        b_staff = self._make_user("b@test.com", "secret", 2, "staff")
        result = memberships.revoke_membership(owner, 1, b_staff)
        self.assertFalse(result["success"])
        self.assertTrue(self._membership_exists(b_staff, 2))


if __name__ == "__main__":
    unittest.main()
