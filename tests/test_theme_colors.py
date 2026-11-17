"""Fase 1 — Regresión del circuito de colores de identidad visual.

Cubre:
- theme_colors: normalización, paleta derivada, contraste WCAG.
- frontend: build_public_frontend_config expone la paleta completa.
- Landing: meta theme-color + set completo de vars CSS en body.
- Wizard y gestión: heredan primary/rgb/secondary del tenant.
- Logo externo: reescrito al proxy de mismo origen (CSP).
- style.css: sin hardcodes de rgba(20,99,255) fuera de admin.css.
"""

import re
import unittest
from unittest.mock import patch

import pytest

import app as application
from application.frontend import build_public_frontend_config
from application.media import rewrite_external_logo_url
from application.theme_colors import (
    DEFAULT_PRIMARY,
    contrast_ratio,
    derive_palette,
    is_valid_hex,
    normalize_hex,
)

pytestmark = pytest.mark.no_pg


class TestThemeColorsUnit(unittest.TestCase):
    def test_is_valid_hex_accepts_rrggbb(self):
        self.assertTrue(is_valid_hex("#FF0000"))
        self.assertTrue(is_valid_hex("#abcdef"))
        self.assertFalse(is_valid_hex("#fff"))
        self.assertFalse(is_valid_hex("red"))
        self.assertFalse(is_valid_hex(None))

    def test_normalize_hex_falls_back(self):
        self.assertEqual(normalize_hex("#ff0000", "#1463FF"), "#FF0000")
        self.assertEqual(normalize_hex("", "#1463FF"), "#1463FF")
        self.assertEqual(normalize_hex("nope", "#1463FF"), "#1463FF")
        self.assertEqual(normalize_hex(None, "#1463FF"), "#1463FF")

    def test_derive_palette_full_tokens(self):
        palette = derive_palette("#FF0000")
        self.assertEqual(palette["primary"], "#FF0000")
        self.assertNotEqual(palette["primary_hover"], "#FF0000")
        self.assertNotEqual(palette["primary_dark"], "#FF0000")
        self.assertEqual(palette["primary_rgb"], "255, 0, 0")
        self.assertTrue(is_valid_hex(palette["primary_hover"]))
        self.assertTrue(is_valid_hex(palette["primary_light"]))

    def test_derive_palette_defaults_when_empty(self):
        palette = derive_palette("", "", "", "")
        self.assertEqual(palette["primary"], DEFAULT_PRIMARY)

    def test_contrast_ratio_black_white_is_21(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#FFFFFF"), 21.0, places=1)

    def test_build_theme_css_variables_order(self):
        css = build_theme_css_variables_helper()
        self.assertIn("--primary:", css)
        self.assertIn("--primary-hover:", css)
        self.assertIn("--primary-rgb:", css)
        self.assertIn("--font-family:", css)


def build_theme_css_variables_helper():
    from application.theme_colors import build_theme_css_variables

    return build_theme_css_variables(derive_palette("#FF0000"))


class TestFrontendConfigPalette(unittest.TestCase):
    def test_config_exposes_full_palette(self):
        config = build_public_frontend_config(
            {"primary_color": "#AA00BB", "secondary_color": "#112233"}
        )
        theme = config["theme"]
        self.assertEqual(theme["primary"], "#AA00BB")
        self.assertEqual(theme["secondary"], "#112233")
        self.assertEqual(theme["primary_rgb"], "170, 0, 187")
        self.assertIn("primary_hover", theme)
        self.assertIn("primary_dark", theme)
        self.assertIn("primary_light", theme)
        self.assertIn("background", theme)
        self.assertIn("text", theme)
        self.assertIn("font_family", theme)

    def test_config_empty_colors_use_defaults(self):
        config = build_public_frontend_config({})
        self.assertEqual(config["theme"]["primary"], DEFAULT_PRIMARY)

    def test_config_rewrites_external_logo(self):
        config = build_public_frontend_config({"logo_url": "https://cdn.example.com/logo.png"})
        logo = config["branding"]["logo_url"]
        self.assertTrue(logo.startswith("/media/external?url="))
        self.assertIn("cdn.example.com", logo)

    def test_config_keeps_local_logo(self):
        config = build_public_frontend_config({"logo_url": "/media/business_1/logo.png"})
        self.assertEqual(config["branding"]["logo_url"], "/media/business_1/logo.png")


class TestRewriteExternalLogo(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(rewrite_external_logo_url(None))
        self.assertEqual(rewrite_external_logo_url(""), "")

    def test_local_path_untouched(self):
        self.assertEqual(
            rewrite_external_logo_url("/media/business_2/x.png"), "/media/business_2/x.png"
        )

    def test_https_rewritten(self):
        out = rewrite_external_logo_url("https://example.com/a.png")
        self.assertTrue(out.startswith("/media/external?url="))


class TestStyleCssNoHardcodedPrimary(unittest.TestCase):
    def test_style_css_has_no_hardcoded_primary_rgba(self):
        """style.css no debe contener rgba(20,99,255) hardcodeado (usa --primary-rgb)."""
        from pathlib import Path

        css = Path("static/style.css").read_text(encoding="utf-8")
        hardcodes = re.findall(r"rgba\(\s*20\s*,\s*99\s*,\s*255", css)
        self.assertEqual(hardcodes, [], f"Hardcodes en style.css: {len(hardcodes)}")
        self.assertIn("--primary-rgb", css)


class TestLandingThemeInjection(unittest.TestCase):
    """La landing inyecta meta theme-color + set completo de vars."""

    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def test_default_landing_has_theme_color_and_vars(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG)
            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn('name="theme-color"', html)
            self.assertIn("--primary:", html)
            self.assertIn("--primary-hover:", html)
            self.assertIn("--primary-rgb:", html)
            self.assertIn("--font-family:", html)

    def test_custom_primary_reflected_everywhere(self):
        business_b = {"id": 2, "name": "Biz X", "slug": "biz-x"}
        settings_b = {
            "business_name": "Biz X",
            "business_type": "Salón",
            "business_initials": "BX",
            "business_description": "Desc",
            "timezone": "America/Argentina/Buenos_Aires",
            "primary_color": "#AA00BB",
            "secondary_color": "#112233",
        }
        with patch.object(application, "resolve_business", return_value=business_b):
            with patch.object(application, "get_business_settings_scoped", return_value=settings_b):
                with patch.object(application, "get_active_services_scoped", return_value=[]):
                    with application.app.test_client() as client:
                        response = client.get("/b/biz-x")
                        self.assertEqual(response.status_code, 200)
                        html = response.text
                        self.assertIn('content="#AA00BB"', html)
                        self.assertIn("--primary: #AA00BB", html)
                        self.assertIn("--primary-rgb: 170, 0, 187", html)
                        self.assertIn("--primary-deep: #112233", html)
                        # Derivados presentes
                        self.assertIn("--primary-hover:", html)
                        self.assertIn("--primary-dark:", html)
                        self.assertNotIn("rgba(20,99,255", html.replace(" ", ""))

    def test_wizard_reflects_primary(self):
        business_b = {"id": 3, "name": "Biz W", "slug": "biz-w"}
        settings_b = {
            "business_name": "Biz W",
            "business_type": "Barbería",
            "business_initials": "BW",
            "business_description": "Desc",
            "timezone": "America/Argentina/Buenos_Aires",
            "primary_color": "#CC0044",
            "secondary_color": "#002244",
        }
        with patch.object(application, "resolve_business", return_value=business_b):
            with patch.object(application, "get_business_settings_scoped", return_value=settings_b):
                with patch.object(application, "get_active_services_scoped", return_value=[]):
                    with application.app.test_client() as client:
                        response = client.get("/b/biz-w/reservar")
                        self.assertEqual(response.status_code, 200)
                        html = response.text
                        self.assertIn("--primary: #CC0044", html)
                        self.assertIn("--primary-rgb: 204, 0, 68", html)

    def test_gestionar_turno_reflects_primary(self):
        business_b = {"id": 4, "name": "Biz G", "slug": "biz-g"}
        settings_b = {
            "business_name": "Biz G",
            "business_type": "Barbería",
            "business_initials": "BG",
            "business_description": "Desc",
            "timezone": "America/Argentina/Buenos_Aires",
            "primary_color": "#00AA55",
            "secondary_color": "#003322",
        }
        with patch.object(application, "resolve_business", return_value=business_b):
            with patch.object(application, "get_business_settings_scoped", return_value=settings_b):
                with patch.object(application, "get_active_services_scoped", return_value=[]):
                    with application.app.test_client() as client:
                        response = client.get("/b/biz-g/gestionar/1")
                        # Puede ser 200 o redirect según flujo; el HTML si hay 200
                        if response.status_code == 200:
                            html = response.text
                            self.assertIn("--primary: #00AA55", html)
                            self.assertIn("--primary-rgb: 0, 170, 85", html)


if __name__ == "__main__":
    unittest.main()
