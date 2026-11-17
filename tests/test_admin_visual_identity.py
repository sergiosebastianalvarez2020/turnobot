"""Fase 4 — Editor de identidad visual en el panel admin + vista previa.

Cubre:
- Sección editor en /b/<slug>/admin (colores, font, media, preset, locks).
- Guardar (enforce_locks=True), preset, y rechazos (config inválida, contraste).
- Vista previa: draft en sesión (nunca persistido), página shell con iframes,
  frame que renderiza landing.html con la config del draft, 404 sin draft,
  aislamiento por tenant y CSRF obligatorio.
"""

import re
import unittest

import pytest

import app as application
from database.database import get_connection
from services import platform as platform_service
from services import visual_identity as vi

pytestmark = pytest.mark.no_pg

OWNER_EMAIL = "owner@test-av.com"
OWNER_PASSWORD = "clave-segura-123"
SLUG = "test-av-biz"


def _csrf_from(page):
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    return match.group(1) if match else None


def _provision(slug=SLUG, email=OWNER_EMAIL):
    result = platform_service.provision_business("AV Biz", slug, email)
    approved = platform_service.approve_business(result["business_id"])
    token = approved["invitation_url"].rsplit("/", 1)[-1]
    platform_service.accept_invitation(result["business_id"], token, OWNER_PASSWORD)
    return result["business_id"]


@pytest.mark.usefixtures("sqlite_temp_db")
class TestAdminVisualIdentity(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.client = application.app.test_client()
        self.business_id = _provision()
        page = self.client.get(f"/b/{SLUG}/login")
        csrf = _csrf_from(page)
        self.client.post(
            f"/b/{SLUG}/login",
            data={"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "csrf_token": csrf},
        )

    def _admin_page(self):
        return self.client.get(f"/b/{SLUG}/admin")

    def _post(self, path, extra=None):
        page = self._admin_page()
        data = {"csrf_token": _csrf_from(page)}
        data.update(extra or {})
        return self.client.post(f"/b/{SLUG}/{path}", data=data)

    # ---- editor ------------------------------------------------------------

    def test_editor_section_visible(self):
        page = self._admin_page()
        self.assertEqual(page.status_code, 200)
        self.assertIn("Identidad visual", page.text)
        self.assertIn('name="page_background_color"', page.text)
        self.assertIn('name="font_family"', page.text)
        self.assertIn('name="cover_image_url"', page.text)
        self.assertIn("/admin/identidad/guardar", page.text)
        self.assertIn("/admin/identidad/preset", page.text)
        self.assertIn("/admin/identidad/preview", page.text)

    def test_editor_muestra_campo_bloqueado_deshabilitado(self):
        vi.save_locked_options(self.business_id, ["primary_color"])
        page = self._admin_page()
        matches = re.findall(r'name="primary_color"([^>]*)>', page.text)
        self.assertTrue(matches)
        self.assertTrue(
            any("disabled" in attrs for attrs in matches),
            f"ningún input primary_color está disabled: {matches}",
        )

    # ---- guardar -----------------------------------------------------------

    def test_guardar_persiste(self):
        resp = self._post(
            "admin/identidad/guardar",
            {
                "primary_color": "#0F766E",
                "page_background_color": "#F0FDFA",
                "page_text_color": "#134E4A",
                "font_family": "Work Sans",
                "logo_url": "/media/x/logo.png",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("identidad_message=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#0F766E")
        self.assertEqual(config["logo_url"], "/media/x/logo.png")

    def test_guardar_espeja_a_business_settings(self):
        self._post("admin/identidad/guardar", {"primary_color": "#0F766E"})
        c = get_connection()
        try:
            row = c.execute(
                "SELECT primary_color FROM business_settings WHERE business_id = ?",
                (self.business_id,),
            ).fetchone()
        finally:
            c.close()
        self.assertEqual(row["primary_color"], "#0F766E")

    def test_guardar_respeta_locks(self):
        vi.save_locked_options(self.business_id, ["primary_color"])
        resp = self._post("admin/identidad/guardar", {"primary_color": "#AA0000"})
        self.assertEqual(resp.status_code, 302)
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#1463FF")

    def test_guardar_contraste_rechazado(self):
        resp = self._post(
            "admin/identidad/guardar",
            {"page_text_color": "#777777", "page_background_color": "#888888"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("identidad_error=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["page_text_color"], "#102A56")

    # ---- preset ------------------------------------------------------------

    def test_preset_aplica_y_conserva_logo(self):
        self._post("admin/identidad/guardar", {"logo_url": "/media/x/logo.png"})
        resp = self._post("admin/identidad/preset", {"theme_preset": "premium"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("identidad_message=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["theme_preset"], "premium")
        self.assertEqual(config["primary_color"], "#4C1D95")
        self.assertEqual(config["logo_url"], "/media/x/logo.png")

    def test_preset_respeta_locks(self):
        vi.save_locked_options(self.business_id, ["primary_color"])
        resp = self._post("admin/identidad/preset", {"theme_preset": "premium"})
        self.assertEqual(resp.status_code, 302)
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["theme_preset"], "premium")
        self.assertEqual(config["primary_color"], "#1463FF")

    # ---- vista previa ------------------------------------------------------

    def _preview_post(self, extra=None):
        return self._post("admin/identidad/preview", extra or {})

    def test_preview_crea_draft_y_frame_renderiza_landing(self):
        resp = self._preview_post({"primary_color": "#0F766E", "font_family": "Work Sans"})
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/admin/identidad/preview", location)
        self.assertNotIn("guardar", location)

        # El draft no se persiste.
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#1463FF")

        shell = self.client.get(location)
        self.assertEqual(shell.status_code, 200)
        self.assertIn("Vista previa", shell.text)
        self.assertIn("/admin/identidad/preview-frame", shell.text)

        frame = self.client.get(f"/b/{SLUG}/admin/identidad/preview-frame")
        self.assertEqual(frame.status_code, 200)
        # La landing renderizada usa el color del draft.
        self.assertIn("--primary: #0F766E".lower(), frame.text.lower())

    def test_preview_config_invalida_no_crea_draft(self):
        resp = self._preview_post({"logo_url": "no-es-url"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("identidad_error=", resp.headers["Location"])
        frame = self.client.get(f"/b/{SLUG}/admin/identidad/preview-frame")
        self.assertEqual(frame.status_code, 404)

    def test_preview_sin_draft_frame_404_y_pagina_redirige(self):
        frame = self.client.get(f"/b/{SLUG}/admin/identidad/preview-frame")
        self.assertEqual(frame.status_code, 404)
        page = self.client.get(f"/b/{SLUG}/admin/identidad/preview")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/admin", page.headers["Location"])

    def test_draft_aislado_por_tenant(self):
        other_id = _provision(slug="test-av-other", email="owner2@test-av.com")
        with self.client.session_transaction() as sess:
            sess["visual_draft"] = {"business_id": other_id, "config": {"primary_color": "#123456"}}
        frame = self.client.get(f"/b/{SLUG}/admin/identidad/preview-frame")
        self.assertEqual(frame.status_code, 404)

    # ---- seguridad ---------------------------------------------------------

    def test_posts_requieren_csrf(self):
        for path in (
            "admin/identidad/guardar",
            "admin/identidad/preset",
            "admin/identidad/preview",
        ):
            resp = self.client.post(f"/b/{SLUG}/{path}", data={"primary_color": "#1463FF"})
            self.assertEqual(resp.status_code, 400, path)

    def test_anonimo_redirige_a_login(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        for path in ("admin/identidad/preview", "admin/identidad/preview-frame"):
            resp = self.client.get(f"/b/{SLUG}/{path}")
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login", resp.headers.get("Location", ""))
