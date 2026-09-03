"""Pruebas de los helpers scoped para gestión de memberships (PASO 1).

Verifica aislamiento estricto por negocio de:
- list_members_scoped
- change_membership_role_scoped
- revoke_membership_scoped
- count_owners_scoped
"""

import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

import database.database as database
from database.database import get_connection


class MembershipManagementTests(unittest.TestCase):
    """Negocio 1 y negocio 2 en base temporal, sin alterar la DB real."""

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

    def _role_id(self, name):
        return database.get_role_id_scoped(name)

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


class TestListMembers(MembershipManagementTests):

    def test_listar_miembros_de_A_no_muestra_miembros_de_B(self):
        a1 = self._make_user("a1@test.com", "secreta1", 1, "owner")
        a2 = self._make_user("a2@test.com", "secreta2", 1, "staff")
        self._make_user("b1@test.com", "secreta3", 2, "owner")

        members_a = database.list_members_scoped(1)
        emails_a = {row["email"] for row in members_a}
        self.assertIn("a1@test.com", emails_a)
        self.assertIn("a2@test.com", emails_a)
        self.assertNotIn("b1@test.com", emails_a)
        self.assertTrue(all(row["business_id"] == 1 for row in members_a))

        members_b = database.list_members_scoped(2)
        self.assertEqual({row["email"] for row in members_b}, {"b1@test.com"})


class TestChangeMembershipRole(MembershipManagementTests):

    def test_cambiar_rol_en_A_no_cambia_rol_en_B(self):
        user_id = self._make_user("x@test.com", "secreta", 1, "owner")
        database.create_membership_scoped(user_id, 2, "admin")

        changed = database.change_membership_role_scoped(
            user_id, 1, self._role_id("staff")
        )
        self.assertTrue(changed)
        self.assertEqual(self._role_of(user_id, 1), self._role_id("staff"))
        self.assertEqual(self._role_of(user_id, 2), self._role_id("admin"))

    def test_cambiar_rol_user_de_otro_tenant_no_afecta_al_mismo(self):
        # El usuario solo pertenece al negocio 2; cambiar su rol en el
        # negocio 1 no debe producir cambios (no existe membership ahí).
        user_id = self._make_user("y@test.com", "secreta", 2, "owner")

        changed = database.change_membership_role_scoped(
            user_id, 1, self._role_id("staff")
        )
        self.assertFalse(changed)
        self.assertEqual(self._role_of(user_id, 2), self._role_id("owner"))

    def test_change_membership_role_rechaza_rol_inexistente_sin_cambios(self):
        user_id = self._make_user("z@test.com", "secreta", 1, "owner")
        changed = database.change_membership_role_scoped(user_id, 1, 9999)
        self.assertFalse(changed)
        # No debe quedar raíz con FK inválida: la fila sigue igual.
        self.assertEqual(self._role_of(user_id, 1), self._role_id("owner"))


class TestRevokeMembership(MembershipManagementTests):

    def test_revocar_en_A_no_elimina_membership_en_B(self):
        user_id = self._make_user("x@test.com", "secreta", 1, "owner")
        database.create_membership_scoped(user_id, 2, "admin")

        revoked = database.revoke_membership_scoped(user_id, 1)
        self.assertTrue(revoked)
        self.assertFalse(self._membership_exists(user_id, 1))
        self.assertTrue(self._membership_exists(user_id, 2))

    def test_revocar_user_de_otro_tenant_no_toca_al_mismo(self):
        user_id = self._make_user("y@test.com", "secreta", 2, "owner")

        revoked = database.revoke_membership_scoped(user_id, 1)
        self.assertFalse(revoked)
        self.assertTrue(self._membership_exists(user_id, 2))

    def test_revocar_membership_inexistente_devuelve_false(self):
        self._make_user("z@test.com", "secreta", 1, "owner")
        revoked = database.revoke_membership_scoped(999999, 1)
        self.assertFalse(revoked)


class TestCountOwners(MembershipManagementTests):

    def test_cuenta_solo_owners_del_negocio_indicado(self):
        self._make_user("a1@test.com", "secreta1", 1, "owner")
        self._make_user("a2@test.com", "secreta2", 1, "owner")
        self._make_user("a3@test.com", "secreta3", 1, "staff")
        self._make_user("b1@test.com", "secreta4", 2, "owner")

        self.assertEqual(database.count_owners_scoped(1), 2)
        self.assertEqual(database.count_owners_scoped(2), 1)

    def test_count_owners_scoped_cero_sin_owners(self):
        self._make_user("a1@test.com", "secreta1", 1, "admin")
        self.assertEqual(database.count_owners_scoped(1), 0)


if __name__ == "__main__":
    unittest.main()
