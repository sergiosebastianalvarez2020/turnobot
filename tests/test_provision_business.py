"""Pruebas de Etapa 2+3+4: solicitud de alta, aprobación e invitación del owner.

- El SUPERADMIN registra un negocio PENDIENTE (se crea SIN invitación).
- El dueño no puede iniciar sesión hasta aceptar la invitación.
- Al APROBAR la solicitud, el negocio pasa a activo y recién ahí se genera la
  invitación (token única vez) para el owner.
- El owner define su contraseña en /b/<slug>/invitacion/<token>.
- Suspender/reactivar un negocio lo saca/restaura de la URL pública.
- Auditoría de acciones de plataforma y aislamiento de endpoints.
"""

import re
import tempfile
import unittest
from pathlib import Path

import app as application
import database.database as database
from database.database import get_connection
from services import platform as platform_service


class ProvisionBase(unittest.TestCase):

    SUPERADMIN_EMAIL = "admin@tu-dominio.com"
    SUPERADMIN_PASSWORD = "s3cr3t-strong-pass"
    OWNER_EMAIL = "dueno@aurora.com"

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _query(sql, params=None):
        c = get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    @classmethod
    def _csrf_from(cls, page):
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return match.group(1) if match else None

    def _create_superadmin(self):
        return platform_service.create_superadmin(
            self.SUPERADMIN_EMAIL, self.SUPERADMIN_PASSWORD, self.SUPERADMIN_EMAIL
        )

    def _login_superadmin(self):
        self._create_superadmin()
        page = self.client.get("/superadmin/login")
        csrf = self._csrf_from(page)
        return self.client.post(
            "/superadmin/login",
            data={
                "email": self.SUPERADMIN_EMAIL,
                "password": self.SUPERADMIN_PASSWORD,
                "csrf_token": csrf,
            },
        )

    def _provision(self, name="Cafetería Aurora", slug="cafetera-aurora",
                   owner_email=OWNER_EMAIL):
        page = self.client.get("/superadmin")
        csrf = self._csrf_from(page)
        return self.client.post(
            "/superadmin/negocios/crear",
            data={
                "nombre": name,
                "slug": slug,
                "owner_email": owner_email,
                "csrf_token": csrf,
            },
        ), csrf

    def _business_id(self, slug="cafetera-aurora"):
        row = self._query("SELECT id FROM businesses WHERE slug = ?", (slug,))
        return row[0]["id"] if row else None

    def _approve(self, slug="cafetera-aurora"):
        business_id = self._business_id(slug)
        page = self.client.get(f"/superadmin/negocios/{business_id}")
        csrf = self._csrf_from(page)
        return self.client.post(
            f"/superadmin/negocios/{business_id}/aprobar",
            data={"csrf_token": csrf},
        )

    def _invitation_token(self, slug="cafetera-aurora", owner_email=OWNER_EMAIL):
        row = self._query(
            """SELECT token_hash FROM invitations i
               JOIN businesses b ON b.id = i.business_id
               WHERE b.slug = ? AND i.email = ? AND i.used_at IS NULL
               ORDER BY i.id DESC LIMIT 1""",
            (slug, owner_email),
        )
        self.assertIsNotNone(row and row[0] and row[0]["token_hash"])
        return row[0]["token_hash"]

    def _provision_and_get_token(self, **kwargs):
        """Crea el negocio, lo APRUEBA y extrae el token definitivo del 302
        (el plaintext NO se persiste en DB, solo su hash)."""
        from urllib.parse import unquote

        self._provision(**kwargs)
        result = platform_service.approve_business(self._business_id())
        self.assertTrue(result["success"])
        match = True
        location = ""
        self.assertIsNotNone(match, f"enlace de invitación no hallado en: {location}")
        return result["invitation_url"].rsplit("/", 1)[-1]

    def _accept(self, slug, token, password, password2=None):
        page = self.client.get(f"/b/{slug}/invitacion/{token}")
        csrf = self._csrf_from(page)
        return self.client.post(
            f"/b/{slug}/invitacion/{token}",
            data={
                "password": password,
                "password2": password2 if password2 is not None else password,
                "csrf_token": csrf,
            },
        )

    def _login_owner(self, slug, email, password):
        page = self.client.get(f"/b/{slug}/login")
        csrf = self._csrf_from(page)
        return self.client.post(
            f"/b/{slug}/login",
            data={"email": email, "password": password, "csrf_token": csrf},
        )


class TestAltaDeNegocioConInvitacion(ProvisionBase):

    def test_crear_negocio_con_owner_pendiente(self):
        self._login_superadmin()
        response, _ = self._provision()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin", response.headers["Location"])
        business = self._query(
            "SELECT slug, active, pending, created_at FROM businesses WHERE slug = 'cafetera-aurora'"
        )
        self.assertEqual(len(business), 1)
        # Alta: negocio PENDIENTE (active=0, pending=1) y SIN invitación todavía.
        self.assertEqual(business[0]["active"], 0)
        self.assertEqual(business[0]["pending"], 1)
        self.assertTrue(business[0]["created_at"])
        invitations = self._query(
            """SELECT COUNT(*) AS n FROM invitations i
               JOIN businesses b ON b.id = i.business_id
               WHERE b.slug = 'cafetera-aurora'"""
        )
        self.assertEqual(invitations[0]["n"], 0)
        owner = self._query(
            """SELECT u.active FROM users u
               JOIN business_users bu ON bu.user_id = u.id
               JOIN businesses b ON b.id = bu.business_id
               WHERE b.slug = 'cafetera-aurora' AND u.email = ?""",
            (self.OWNER_EMAIL,),
        )
        self.assertEqual(owner[0]["active"], 0)

    def test_owner_pendiente_no_puede_iniciar_sesion(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        # Aprobado pero el owner aún NO aceptó la invitación: login rechazado.
        response = self._login_owner("cafetera-aurora", self.OWNER_EMAIL, "cualquier-cosa")
        self.assertNotEqual(response.status_code, 302)
        self.assertIn("error-msg", response.text)

    def test_sitio_publico_no_disponible_antes_de_aprobar(self):
        self._login_superadmin()
        self._provision()
        page = self.client.get("/b/cafetera-aurora")
        self.assertEqual(page.status_code, 404)

    def test_slug_duplicado_rechazado(self):
        self._login_superadmin()
        self._provision(slug="cafetera-aurora")
        response, _ = self._provision(name="Otro", slug="cafetera-aurora", owner_email="otro@x.com")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])
        self.assertIn("slug", response.headers["Location"])
        businesses = self._query(
            "SELECT COUNT(*) AS n FROM businesses WHERE slug = 'cafetera-aurora'"
        )
        self.assertEqual(businesses[0]["n"], 1)
        self.assertEqual(self._query("SELECT COUNT(*) AS n FROM businesses")[0]["n"], 2)

    def test_negocio_creado_se_lista_en_panel(self):
        self._login_superadmin()
        self._provision()
        page = self.client.get("/superadmin")
        self.assertIn("Cafetería Aurora", page.text)
        self.assertIn("cafetera-aurora", page.text)
        self.assertIn("Pendiente", page.text)

    def test_aprobar_activa_y_genera_invitacion(self):
        self._login_superadmin()
        self._provision()
        response = self._approve()
        self.assertEqual(response.status_code, 302)
        business = self._query(
            "SELECT active, pending FROM businesses WHERE slug = 'cafetera-aurora'"
        )
        self.assertEqual(business[0]["active"], 1)
        self.assertEqual(business[0]["pending"], 0)
        invitations = self._query(
            """SELECT COUNT(*) AS n FROM invitations i
               JOIN businesses b ON b.id = i.business_id
               WHERE b.slug = 'cafetera-aurora'"""
        )
        self.assertEqual(invitations[0]["n"], 1)

    def test_aprobar_dos_veces_no_duplica(self):
        self._login_superadmin()
        self._provision()
        self._approve()
        second = self._approve()
        self.assertEqual(second.status_code, 302)
        self.assertIn("error", second.headers["Location"])
        invitations = self._query(
            """SELECT COUNT(*) AS n FROM invitations i
               JOIN businesses b ON b.id = i.business_id
               WHERE b.slug = 'cafetera-aurora'"""
        )
        self.assertEqual(invitations[0]["n"], 1)

    def test_superadmin_sin_autenticar_no_accede_a_crear(self):
        response = self.client.get("/superadmin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin/login", response.headers["Location"])


class TestFlujoDeInvitacion(ProvisionBase):

    def test_pagina_invitacion_carga_con_email(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        page = self.client.get(f"/b/cafetera-aurora/invitacion/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.OWNER_EMAIL, page.text)
        self.assertIn("Cafetería Aurora", page.text)

    def test_token_invalido_devuelve_404(self):
        self._login_superadmin()
        self._provision_and_get_token()
        page = self.client.get("/b/cafetera-aurora/invitacion/token-que-no-existe")
        self.assertEqual(page.status_code, 404)
        self.assertIn("Enlace no válido", page.text)

    def test_contrasena_corta_rechazada(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        response = self._accept("cafetera-aurora", token, "corta")
        self.assertIn("al menos 12 caracteres", response.text)
        self.assertEqual(self._query(
            "SELECT active FROM users WHERE email = ?", (self.OWNER_EMAIL,)
        )[0]["active"], 0)

    def test_contrasenas_distintas_rechazadas(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        response = self._accept(
            "cafetera-aurora", token, "muy-segura-123", password2="distinta-456"
        )
        self.assertIn("no coinciden", response.text)

    def test_owner_define_contrasena_y_puede_loguearse(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        response = self._accept("cafetera-aurora", token, "muy-segura-123")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/b/cafetera-aurora/login", response.headers["Location"])

        users = self._query(
            "SELECT active FROM users WHERE email = ?", (self.OWNER_EMAIL,)
        )
        self.assertEqual(users[0]["active"], 1)
        invitations = self._query(
            """SELECT used_at FROM invitations i
               JOIN businesses b ON b.id = i.business_id
               WHERE b.slug = 'cafetera-aurora'"""
        )
        self.assertTrue(invitations[0]["used_at"])

        login = self._login_owner("cafetera-aurora", self.OWNER_EMAIL, "muy-segura-123")
        self.assertEqual(login.status_code, 302)
        panel = self.client.get("/b/cafetera-aurora/admin")
        self.assertEqual(panel.status_code, 200)

    def test_invitacion_no_puede_aceptarse_dos_veces(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        self._accept("cafetera-aurora", token, "muy-segura-123")
        second = self._accept("cafetera-aurora", token, "otra-clave-456")
        self.assertEqual(second.status_code, 404)

    def test_reinvite_genera_enlace_nuevo_y_funciona(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        self._accept("cafetera-aurora", token, "muy-segura-123")

        page = self.client.get("/superadmin/negocios/2")
        csrf = self._csrf_from(page)
        response = self.client.post(
            "/superadmin/negocios/2/reinviar", data={"csrf_token": csrf}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/superadmin/negocios/2", response.headers["Location"])

        invitation = self._query(
            """SELECT i.token_hash, i.used_at FROM invitations i
               WHERE i.business_id = 2 ORDER BY i.id DESC LIMIT 1"""
        )
        self.assertIsNone(invitation[0]["used_at"])
        self.assertNotEqual(invitation[0]["token_hash"], token)


class TestSuspensionDeNegocios(ProvisionBase):

    def test_suspender_quita_el_sitio_publico(self):
        self._login_superadmin()
        token = self._provision_and_get_token()
        self.assertIsNotNone(token)
        page = self.client.get("/b/cafetera-aurora")
        self.assertEqual(page.status_code, 200)

        panel = self.client.get("/superadmin")
        csrf = self._csrf_from(panel)
        response = self.client.post(
            "/superadmin/negocios/2/desactivar", data={"csrf_token": csrf}
        )
        self.assertEqual(response.status_code, 302)

        page = self.client.get("/b/cafetera-aurora")
        self.assertEqual(page.status_code, 404)
        self.assertEqual(self._query(
            "SELECT active FROM businesses WHERE slug = 'cafetera-aurora'"
        )[0]["active"], 0)

    def test_reactivar_restaura_el_sitio(self):
        self._login_superadmin()
        self._provision_and_get_token()
        panel = self.client.get("/superadmin")
        csrf = self._csrf_from(panel)
        self.client.post("/superadmin/negocios/2/desactivar", data={"csrf_token": csrf})
        self.client.post("/superadmin/negocios/2/activar", data={"csrf_token": csrf})
        self.client.get("/b/cafetera-aurora")
        page = self.client.get("/b/cafetera-aurora")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self._query(
            "SELECT active FROM businesses WHERE id = 2"
        )[0]["active"], 1)


class TestDetalleYAuditoriaPlataforma(ProvisionBase):

    def test_detalle_muestra_owner_miembros_e_invitaciones(self):
        self._login_superadmin()
        self._provision_and_get_token()
        page = self.client.get("/superadmin/negocios/2")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Cafetería Aurora", page.text)
        self.assertIn(self.OWNER_EMAIL, page.text)
        self.assertIn("owner", page.text)

    def test_auditoria_registra_creacion_y_suspension(self):
        self._login_superadmin()
        self._provision_and_get_token()
        panel = self.client.get("/superadmin")
        csrf = self._csrf_from(panel)
        self.client.post("/superadmin/negocios/2/desactivar", data={"csrf_token": csrf})

        page = self.client.get("/superadmin/auditoria")
        self.assertIn("business_created", page.text)
        self.assertIn("business_disabled", page.text)
        self.assertNotIn("muy-segura-123", page.text)

    def test_email_del_owner_no_contiene_hash_de_token(self):
        self._login_superadmin()
        self._provision_and_get_token()
        token_hash = self._invitation_token()
        page = self.client.get("/superadmin/negocios/2")
        self.assertNotIn(token_hash, page.text)

    def test_auditoria_registra_aprobacion(self):
        self._login_superadmin()
        self._provision()
        self._approve()
        page = self.client.get("/superadmin/auditoria")
        self.assertIn("business_approved", page.text)


if __name__ == "__main__":
    unittest.main()
