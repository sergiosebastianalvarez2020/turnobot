"""Fase 5 — La identidad visual se aplica en TODAS las páginas públicas /b/<slug>.

Cubre:
- Landing, wizard (paso 1) y gestionar_turno con la config de identidad visual
  (no solo legacy primary/secondary: background, texto y tipografía).
- Login (/b/<slug>/login) con la identidad del negocio.
- Invitación pública con la identidad del negocio.
- Sin identidad guardada: el branding legacy de business_settings manda.
"""

import re
import unittest

import pytest

import app as application
from services import platform as platform_service
from services import visual_identity as vi

pytestmark = pytest.mark.no_pg

OWNER_EMAIL = "owner@test-w5.com"
SLUG = "test-w5-biz"


def _csrf_from(page):
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    return match.group(1) if match else None


def _provision(slug=SLUG, email=OWNER_EMAIL):
    result = platform_service.provision_business("W5 Biz", slug, email)
    approved = platform_service.approve_business(result["business_id"])
    token = approved["invitation_url"].rsplit("/", 1)[-1]
    return result["business_id"], token


@pytest.mark.usefixtures("sqlite_temp_db")
class TestPublicPagesIdentity(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.client = application.app.test_client()
        self.business_id, self.invitation_token = _provision()

    def _identity(self):
        vi.save_identity(
            self.business_id,
            {
                "primary_color": "#7C2D12",
                "secondary_color": "#431407",
                "page_background_color": "#FFF8F0",
                "page_text_color": "#292524",
                "font_family": "Work Sans",
            },
        )

    def _slug_client_get(self, path):
        return self.client.get(f"/b/{SLUG}{path}")

    def test_landing_refleja_identidad_completa(self):
        self._identity()
        resp = self._slug_client_get("")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--primary: #7C2D12", resp.text)
        self.assertIn("--bg: #FFF8F0", resp.text)
        self.assertIn("--primary-hover:", resp.text)

    def test_wizard_refleja_identidad_completa(self):
        self._identity()
        resp = self._slug_client_get("/reservar")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--primary: #7C2D12", resp.text)
        self.assertIn("--bg: #FFF8F0", resp.text)
        self.assertIn("--primary-hover:", resp.text)

    def test_login_refleja_identidad(self):
        self._identity()
        resp = self._slug_client_get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--primary: #7C2D12", resp.text)
        self.assertIn("--bg: #FFF8F0", resp.text)

    def test_invitacion_refleja_identidad(self):
        self._identity()
        # El token de `_provision` sigue vigente: no se consumió la invitación.
        resp = self.client.get(f"/b/{SLUG}/invitacion/{self.invitation_token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--primary: #7C2D12", resp.text)
        self.assertIn("--bg: #FFF8F0", resp.text)

    def test_sin_identidad_el_legacy_manda(self):
        # Branding legacy en business_settings (sin fila de identidad visual).
        from database.database import update_business_settings_scoped

        update_business_settings_scoped(
            self.business_id,
            "W5 Biz",
            "Barbería",
            "W5",
            "",
            "UTC",
            primary_color="#0F4C81",
            secondary_color="#082F49",
        )
        resp = self._slug_client_get("")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--primary: #0F4C81", resp.text)

    def test_identidad_pisa_al_legacy(self):
        from database.database import update_business_settings_scoped

        update_business_settings_scoped(
            self.business_id,
            "W5 Biz",
            "Barbería",
            "W5",
            "",
            "UTC",
            primary_color="#0F4C81",
            secondary_color="#082F49",
        )
        self._identity()
        resp = self._slug_client_get("")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--primary: #7C2D12", resp.text)
