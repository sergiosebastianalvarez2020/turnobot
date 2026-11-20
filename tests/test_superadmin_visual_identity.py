"""Fase 3 — Panel Super Admin de identidad visual.

Cubre:
- Gate: anónimo redirige a /superadmin/login; CSRF obligatorio en POSTs.
- Listado /superadmin/identidad con preset y color primario por negocio.
- Detalle /superadmin/identidad/<id>: formulario con colores, fonts y media.
- Guardar config: persiste y redirige con mensaje; rechaza config inválida y
  contraste insuficiente sin persistir.
- Aplicar preset: reemplaza paleta conservando media cargada.
- Bloqueos: guardar opciones y rechazo de opciones desconocidas.
- El Super Admin no está limitado por locks (enforce_locks=False).
"""

import re
import unittest

import pytest

import app as application
from services import platform as platform_service
from services import visual_identity as vi

pytestmark = pytest.mark.no_pg

SUPERADMIN_EMAIL = "admin@tu-dominio.com"
SUPERADMIN_PASSWORD = "s3cr3t-strong-pass"


def _csrf_from(page):
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    return match.group(1) if match else None


def _provision(slug="test-vi-sa", email="owner@test-vi-sa.com", password="clave-segura-123"):
    result = platform_service.provision_business("Visual SA Biz", slug, email)
    approved = platform_service.approve_business(result["business_id"])
    token = approved["invitation_url"].rsplit("/", 1)[-1]
    platform_service.accept_invitation(result["business_id"], token, password)
    return result["business_id"]


@pytest.mark.usefixtures("sqlite_temp_db")
class TestSuperadminVisualIdentity(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.client = application.app.test_client()
        platform_service.create_superadmin(SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD, SUPERADMIN_EMAIL)
        page = self.client.get("/superadmin/login")
        csrf = _csrf_from(page)
        resp = self.client.post(
            "/superadmin/login",
            data={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD, "csrf_token": csrf},
        )
        assert resp.status_code == 302, resp.status_code
        self.business_id = _provision()

    # ---- gate -------------------------------------------------------------

    def test_anonimo_redirige_a_login(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        for url in ("/superadmin/identidad", f"/superadmin/identidad/{self.business_id}"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/superadmin/login", resp.headers.get("Location", ""))

    def test_posts_requieren_csrf(self):
        for url in (
            f"/superadmin/identidad/{self.business_id}/guardar",
            f"/superadmin/identidad/{self.business_id}/preset",
            f"/superadmin/identidad/{self.business_id}/bloqueos",
        ):
            resp = self.client.post(url, data={"primary_color": "#1463FF"})
            self.assertEqual(resp.status_code, 400, url)

    # ---- listado ----------------------------------------------------------

    def test_listado_muestra_preset_y_primario(self):
        resp = self.client.get("/superadmin/identidad")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Identidad visual por negocio", resp.text)
        self.assertIn("moderno", resp.text)
        self.assertIn("#1463FF", resp.text)

    # ---- detalle ----------------------------------------------------------

    def test_detalle_muestra_formulario(self):
        resp = self.client.get(f"/superadmin/identidad/{self.business_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"/superadmin/identidad/{self.business_id}/guardar", resp.text)
        self.assertIn('name="primary_color"', resp.text)
        self.assertIn('name="font_family"', resp.text)
        self.assertIn('name="logo_url"', resp.text)
        self.assertIn('name="locked_options"', resp.text)
        self.assertIn("minimalista", resp.text)

    def test_detalle_muestra_muestras_hex_paleta_y_preview(self):
        resp = self.client.get(f"/superadmin/identidad/{self.business_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.count("data-vi-color-field"), 6)
        self.assertEqual(resp.text.count('class="vi-color-hex"'), 6)
        for key in (
            "primary_color",
            "secondary_color",
            "page_background_color",
            "surface",
            "page_text_color",
        ):
            self.assertIn(f'data-vi-palette-item="{key}"', resp.text)
        self.assertIn("data-vi-preview", resp.text)
        self.assertIn("vi-preview-button", resp.text)
        self.assertIn("visual_identity.css", resp.text)
        self.assertIn("visual_identity.js", resp.text)

    def test_detalle_muestra_presets_con_paleta_completa(self):
        resp = self.client.get(f"/superadmin/identidad/{self.business_id}")
        self.assertEqual(len(re.findall(r'class="vi-preset-card"', resp.text)), 8)
        self.assertIn('data-preset-name="premium"', resp.text)
        self.assertIn('data-primary-color="#4C1D95"', resp.text)
        self.assertIn("Aplicar preset", resp.text)

    def test_detalle_404(self):
        resp = self.client.get("/superadmin/identidad/999999")
        self.assertEqual(resp.status_code, 404)

    # ---- guardar ----------------------------------------------------------

    def _post_save(self, extra=None, csrf=None):
        page = self.client.get(f"/superadmin/identidad/{self.business_id}")
        data = {"csrf_token": csrf or _csrf_from(page)}
        data.update(extra or {})
        return self.client.post(f"/superadmin/identidad/{self.business_id}/guardar", data=data)

    def test_guardar_persiste_y_redirige(self):
        resp = self._post_save(
            {
                "primary_color": "#0F766E",
                "secondary_color": "#134E4A",
                "page_background_color": "#F0FDFA",
                "page_text_color": "#134E4A",
                "button_bg_color": "#0F766E",
                "button_text_color": "#FFFFFF",
                "font_family": "Work Sans",
                "header_overlay_opacity": "0.4",
                "logo_url": "/media/x/logo.png",
            }
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("message=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#0F766E")
        self.assertEqual(config["logo_url"], "/media/x/logo.png")

    def test_guardar_color_invalido_no_persiste(self):
        resp = self._post_save({"primary_color": "not-a-color"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#1463FF")

    def test_guardar_contraste_insuficiente_rechazado(self):
        resp = self._post_save({"page_text_color": "#777777", "page_background_color": "#888888"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["page_text_color"], "#102A56")

    # ---- preset -----------------------------------------------------------

    def _post_preset(self, preset):
        page = self.client.get(f"/superadmin/identidad/{self.business_id}")
        return self.client.post(
            f"/superadmin/identidad/{self.business_id}/preset",
            data={"csrf_token": _csrf_from(page), "theme_preset": preset},
        )

    def test_preset_reemplaza_paleta_conserva_logo(self):
        self._post_save({"logo_url": "/media/x/logo.png"})
        resp = self._post_preset("premium")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("message=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["theme_preset"], "premium")
        self.assertEqual(config["primary_color"], "#4C1D95")
        self.assertEqual(config["logo_url"], "/media/x/logo.png")

    def test_preset_desconocido_redirige_error(self):
        resp = self._post_preset("no-existe")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    # ---- bloqueos ---------------------------------------------------------

    def _post_locks(self, options):
        page = self.client.get(f"/superadmin/identidad/{self.business_id}")
        form = {"csrf_token": _csrf_from(page)}
        for opt in options:
            form.setdefault("locked_options", [])
            form["locked_options"] = form["locked_options"] + [opt]
        return self.client.post(f"/superadmin/identidad/{self.business_id}/bloqueos", data=form)

    def test_bloqueos_guardan_y_se_respetan(self):
        resp = self._post_locks(["primary_color"])
        self.assertEqual(resp.status_code, 302)
        self.assertIn("message=", resp.headers["Location"])
        _config, locked = vi.load_identity(self.business_id)
        self.assertEqual(locked, ["primary_color"])

        # El tenant queda limitado: save con enforce_locks=True conserva el valor.
        vi.save_identity(self.business_id, {"primary_color": "#AA0000"})
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#1463FF")

    def test_superadmin_save_ignora_locks(self):
        self._post_locks(["primary_color"])
        resp = self._post_save({"primary_color": "#0F766E"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("message=", resp.headers["Location"])
        config, _locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#0F766E")

    def test_bloqueo_opcion_desconocida_rechazada(self):
        resp = self._post_locks(["campo_inventado"])
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    # ---- auditoría --------------------------------------------------------

    def test_acciones_registradas_en_auditoria(self):
        self._post_save({"primary_color": "#0F766E"})
        self._post_preset("urbano")
        self._post_locks(["font_family"])
        page = self.client.get("/superadmin/auditoria")
        self.assertIn("visual_identity_updated", page.text)
        self.assertIn("visual_identity_preset", page.text)
        self.assertIn("visual_identity_locks_updated", page.text)
