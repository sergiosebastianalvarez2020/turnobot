"""Pruebas del SUPERADMIN de plataforma (Etapa SaaS 1).

La identidad de plataforma (`platform_users`/`platform_sessions`) es SEPARADA
de la identidad de negocio (`users`/`business_users`/membresías). Cubre:

- Bootstrap por CLI/servicio (sin contraseña en código).
- Login/logout de superadmin con sesión persistente y CSRF.
- Panel /superadmin y /superadmin/auditoria.
- Aislamiento bidireccional: un admin de negocio NO entra a /superadmin y el
  superadmin NO entra al panel de un negocio (no tiene membresía).
- Auditoría de acciones de plataforma.
- Logout selectivo: cerrar un contexto no destruye el otro.

Ejecuta contra PostgreSQL mediante `tests._pg_compat.PostgreSQLTestCase`: cada test
recibe una base `turnobot_test_<uuid>` desechable con la semilla estándar (negocio 1
"El Corte"). No hay swap de `DATABASE_PATH` ni SQLite; el aislamiento entre tests es
el de la base temporal. La comprobacion `WHERE revoked = 1` se mantiene tal cual para
cubrir de punta a punta la reescritura de predicados booleanos (`= 1` -> `IS TRUE`).
"""

import re
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection
from services import platform as platform_service
from tests._pg_compat import PostgreSQLTestCase


class PlatformBase(unittest.TestCase, PostgreSQLTestCase):
    """Base con la semilla estandar (negocio 1 "El Corte") y helpers de superadmin."""

    SUPERADMIN_EMAIL = "admin@tu-dominio.com"
    SUPERADMIN_PASSWORD = "s3cr3t-strong-pass"

    def setUp(self):
        application.rate_limit_state.clear()
        # `self.client` lo aporta la fixture `client`: app y base temporal propias
        # de este test. Sin ella los 18 tests comparten la base de sesion y el
        # aislamiento entre clases es inexistente.
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.addCleanup(self._app_context.pop)

    # ---- helpers ----------------------------------------------------------

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

    @staticmethod
    def _csrf_from(page):
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return match.group(1) if match else None

    def _create_superadmin(self, email=None, password=None):
        email = email or self.SUPERADMIN_EMAIL
        password = password or self.SUPERADMIN_PASSWORD
        return platform_service.create_superadmin(email, password, email)

    def _login_superadmin(self, email=None, password=None, environ_base=None):
        page = self.client.get("/superadmin/login", follow_redirects=True)
        csrf = self._csrf_from(page)
        post_kwargs = {
            "data": {
                "email": email or self.SUPERADMIN_EMAIL,
                "password": password or self.SUPERADMIN_PASSWORD,
                "csrf_token": csrf,
            }
        }
        if environ_base is not None:
            post_kwargs["environ_base"] = environ_base
        return self.client.post("/superadmin/login", **post_kwargs), csrf

    def _make_tenant_user(self, email, password, business_id=1, role="owner"):
        user_id = database.create_user_scoped(email, generate_password_hash(password), active=True)
        database.create_membership_scoped(user_id, business_id, role)
        return user_id

    def _login_tenant(self, slug, email, password):
        page = self.client.get(f"/b/{slug}/login")
        csrf = self._csrf_from(page)
        return self.client.post(
            f"/b/{slug}/login", data={"email": email, "password": password, "csrf_token": csrf}
        )


class TestSuperadminBootstrapYLogin(PlatformBase):
    def test_superadmin_login_rate_limit_per_ip_and_success_keeps_counter(self):
        self._create_superadmin()
        response, _ = self._login_superadmin(password="incorrecta")
        self.assertEqual(response.status_code, 200)
        response, _ = self._login_superadmin()
        self.assertEqual(response.status_code, 302)
        for _ in range(8):
            response, _ = self._login_superadmin(password="incorrecta")
            self.assertEqual(response.status_code, 200)
        blocked, _ = self._login_superadmin(password="incorrecta")
        self.assertEqual(blocked.status_code, 429)

    def test_superadmin_login_rejects_excessive_attempts_per_ip(self):
        self._create_superadmin()
        for _ in range(10):
            response, _ = self._login_superadmin(password="incorrecta")
            self.assertEqual(response.status_code, 200)
        blocked, _ = self._login_superadmin(password="incorrecta")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Demasiados intentos", blocked.text)

    def test_superadmin_limit_isolated_by_ip_and_blocks_authentication(self):
        auth_calls = []

        def authenticate(*args):
            auth_calls.append(args)
            return None, "Credenciales inválidas."

        with patch.object(platform_service, "authenticate_superadmin", side_effect=authenticate):
            for _ in range(10):
                response, _ = self._login_superadmin(
                    password="incorrecta", environ_base={"REMOTE_ADDR": "192.0.2.10"}
                )
                self.assertEqual(response.status_code, 200)
            self.assertEqual(len(auth_calls), 10)

            blocked, _ = self._login_superadmin(
                password="incorrecta", environ_base={"REMOTE_ADDR": "192.0.2.10"}
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(len(auth_calls), 10)
            self.assertEqual(self.client.get("/superadmin").status_code, 302)

            other_ip, _ = self._login_superadmin(
                password="incorrecta", environ_base={"REMOTE_ADDR": "192.0.2.11"}
            )
            self.assertEqual(other_ip.status_code, 200)
            self.assertEqual(len(auth_calls), 11)

    def test_owner_invitation_link_uses_public_origin_not_request_host(self):
        self._create_superadmin()
        self.app.config["PUBLIC_BASE_URL"] = "https://canonical.example"
        host_headers = {"Host": "evil.example"}

        page = self.client.get("/superadmin/login", headers=host_headers)
        csrf = self._csrf_from(page)
        login = self.client.post(
            "/superadmin/login",
            headers=host_headers,
            data={
                "email": self.SUPERADMIN_EMAIL,
                "password": self.SUPERADMIN_PASSWORD,
                "csrf_token": csrf,
            },
        )
        self.assertEqual(login.status_code, 302)

        page = self.client.get("/superadmin", headers=host_headers)
        csrf = self._csrf_from(page)
        approval = {
            "success": True,
            "slug": "el-corte",
            "owner_email": "owner@example.test",
            "invitation_url": "/b/el-corte/invitacion/fixed-invitation-token",
            "expires_at": "2099-01-01T00:00:00",
        }
        with (
            patch.object(platform_service, "approve_business", return_value=approval),
            patch.object(application, "send_approved_invitation_email") as send_invitation,
        ):
            response = self.client.post(
                "/superadmin/negocios/1/aprobar",
                headers=host_headers,
                data={"csrf_token": csrf},
            )

        self.assertEqual(response.status_code, 302)
        sent_link = send_invitation.call_args.args[2]
        self.assertEqual(
            sent_link,
            "https://canonical.example/b/el-corte/invitacion/fixed-invitation-token",
        )
        self.assertNotIn("evil.example", sent_link)

    def test_create_superadmin_por_servicio_hash_no_plano(self):
        result = self._create_superadmin()
        self.assertTrue(result["success"])
        rows = self._query(
            "SELECT email, password_hash FROM platform_users WHERE email = %s",
            (self.SUPERADMIN_EMAIL,),
        )
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["password_hash"], self.SUPERADMIN_PASSWORD)
        self.assertTrue(rows[0]["password_hash"].startswith(("pbkdf2", "scrypt")))

    def test_create_superadmin_rechaza_password_corta(self):
        result = self._create_superadmin(password="corta")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "weak_password")

    def test_create_superadmin_no_duplica_email(self):
        self._create_superadmin()
        result = self._create_superadmin()
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "email_exists")
        rows = self._query(
            "SELECT COUNT(*) n FROM platform_users WHERE email = %s", (self.SUPERADMIN_EMAIL,)
        )
        self.assertEqual(rows[0]["n"], 1)

    def test_login_superadmin_ok(self):
        self._create_superadmin()
        response, _ = self._login_superadmin()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin", response.headers.get("Location", ""))
        self.assertEqual(self.client.get("/superadmin").status_code, 200)

    def test_login_superadmin_password_incorrecta(self):
        self._create_superadmin()
        response, _ = self._login_superadmin(password="contraseña-incorrecta")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Credenciales inválidas", response.text)

    def test_login_superadmin_requires_csrf(self):
        self._create_superadmin()
        response = self.client.post(
            "/superadmin/login",
            data={"email": self.SUPERADMIN_EMAIL, "password": self.SUPERADMIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 400)

    def test_superadmin_no_se_autentica_contra_usuarios_de_negocio(self):
        # Un usuario de negocio (aunque comparta email) NO es superadmin.
        self._make_tenant_user(self.SUPERADMIN_EMAIL, self.SUPERADMIN_PASSWORD, 1, "owner")
        response, _ = self._login_superadmin()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Credenciales inválidas", response.text)

    def test_misma_direccion_en_ambas_identidades_es_separada(self):
        # El email puede existir en platform_users Y en users sin colisionar.
        self._create_superadmin()
        user_id = self._make_tenant_user(self.SUPERADMIN_EMAIL, "otra-password-segura", 1, "owner")
        self.assertIsNotNone(user_id)
        sa = self._query(
            "SELECT COUNT(*) n FROM platform_users WHERE email = %s", (self.SUPERADMIN_EMAIL,)
        )[0]["n"]
        bu = self._query("SELECT COUNT(*) n FROM users WHERE email = %s", (self.SUPERADMIN_EMAIL,))[
            0
        ]["n"]
        self.assertEqual((sa, bu), (1, 1))


class TestAccesoYPanel(PlatformBase):
    def test_panel_anonimo_redirige_a_login(self):
        response = self.client.get("/superadmin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin/login", response.headers.get("Location", ""))

    def test_panel_muestra_negocios(self):
        self._create_superadmin()
        self._login_superadmin()
        page = self.client.get("/superadmin")
        self.assertEqual(page.status_code, 200)
        self.assertIn("El Corte", page.text)
        self.assertIn("Negocios", page.text)

    def test_auditoria_muestra_logins(self):
        self._create_superadmin()
        self._login_superadmin()
        page = self.client.get("/superadmin/auditoria")
        self.assertEqual(page.status_code, 200)
        self.assertIn("superadmin_login", page.text)

    def test_admin_de_negocio_no_accede_superadmin(self):
        self._create_superadmin()
        self._make_tenant_user("owner@test.com", "secreta-password", 1, "owner")
        self._login_tenant("el-corte", "owner@test.com", "secreta-password")
        # Aunque tenga sesión de negocio activa, /superadmin lo redirige.
        response = self.client.get("/superadmin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin/login", response.headers.get("Location", ""))
        # Y su sesión de negocio NO quedó destruida.
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_superadmin_no_accede_panel_de_negocio(self):
        self._create_superadmin()
        self._login_superadmin()
        # Sin membresía, el superadmin no puede operar ningún negocio.
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        response2 = self.client.get("/b/el-corte/admin")
        self.assertEqual(response2.status_code, 302)


class TestSesionYLogoutSelectivo(PlatformBase):
    def test_logout_superadmin_revoca_sesion_plataforma(self):
        self._create_superadmin()
        self._login_superadmin()
        self.assertEqual(self.client.get("/superadmin").status_code, 200)

        page = self.client.get("/superadmin")
        csrf = self._csrf_from(page)
        response = self.client.post("/superadmin/logout", data={"csrf_token": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin/login", response.headers.get("Location", ""))
        self.assertEqual(
            self._query("SELECT COUNT(*) n FROM platform_sessions WHERE revoked = 1")[0]["n"], 1
        )
        # La sesión HTTP quedó sin credenciales de plataforma.
        follow = self.client.get("/superadmin", follow_redirects=True)
        self.assertNotIn("Negocios", follow.text)

    def test_logout_negocio_no_destruye_sesion_superadmin(self):
        self._create_superadmin()
        self._login_superadmin()
        self._make_tenant_user("owner@test.com", "secreta-password", 1, "owner")
        self._login_tenant("el-corte", "owner@test.com", "secreta-password")

        # El owner cierra su sesión de negocio...
        page = self.client.get("/admin")
        csrf = self._csrf_from(page)
        self.client.post("/logout", data={"csrf_token": csrf})

        # ...pero el superadmin sigue con su sesión de plataforma intacta.
        self.assertEqual(self.client.get("/superadmin").status_code, 200)

    def test_logout_superadmin_no_destruye_sesion_negocio(self):
        self._create_superadmin()
        self._make_tenant_user("owner@test.com", "secreta-password", 1, "owner")
        # Primero la sesión de negocio, luego la de plataforma.
        self._login_tenant("el-corte", "owner@test.com", "secreta-password")
        self._login_superadmin()

        page = self.client.get("/superadmin")
        csrf = self._csrf_from(page)
        self.client.post("/superadmin/logout", data={"csrf_token": csrf})

        self.assertEqual(self.client.get("/admin").status_code, 200)


class TestAuditoria(PlatformBase):
    def test_acciones_quedan_en_audit_log(self):
        self._create_superadmin()
        self.client.get("/superadmin/login")
        self._login_superadmin()
        actions = {r["action"] for r in self._query("SELECT action FROM audit_log ORDER BY id")}
        self.assertTrue({"superadmin_created", "superadmin_login"} <= actions)

    def test_audit_log_no_guarda_secretos(self):
        self._create_superadmin()
        self._login_superadmin()
        rows = self._query("SELECT * FROM audit_log")
        for row in rows:
            # `PgRowProxy` (subclase de dict) itera sobre CLAVES, no sobre valores:
            # con `for v in row` el blob quedaba formado por nombres de columna y
            # estas aserciones no podian fallar nunca (falso positivo silencioso).
            # Hay que leer `.values()` para inspeccionar el contenido real de la fila.
            blob = " ".join(str(v) for v in dict(row).values())
            self.assertNotIn("s3cr3t", blob)
            self.assertNotIn("pbkdf2", blob)


if __name__ == "__main__":
    unittest.main()
