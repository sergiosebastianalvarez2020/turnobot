"""Fase 2 — Identidad visual: migraciones, presets y servicio.

Cubre:
- Migración 026: tabla business_visual_identity con UNIQUE por tenant.
- themes.py: 8 presets, normalización de config, fonts allowlist.
- visual_identity: guardar/cargar, contraste WCAG, locks, espejo a
  business_settings, aislamiento por tenant.
"""

import json
import unittest

import pytest

import app as application
from application.theme_colors import contrast_ratio
from application.themes import (
    CONFIG_KEYS,
    LOCKABLE_OPTIONS,
    THEME_PRESETS,
    default_config,
    get_preset,
    normalize_config,
    preset_names,
)
from database.database import get_connection, get_visual_identity_scoped
from services import visual_identity as vi

pytestmark = pytest.mark.no_pg


def _provision(slug="test-vi-biz", email="owner@test-vi.com", password="clave-segura-123"):
    from services import platform as platform_service

    result = platform_service.provision_business("Visual Biz", slug, email)
    approved = platform_service.approve_business(result["business_id"])
    token = approved["invitation_url"].rsplit("/", 1)[-1]
    platform_service.accept_invitation(result["business_id"], token, password)
    return result["business_id"]


@pytest.mark.usefixtures("sqlite_temp_db")
def test_migration_026_creates_table():
    """La migración 026 crea la tabla con UNIQUE por business_id."""
    c = get_connection()
    try:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='business_visual_identity'"
        ).fetchall()
        assert rows, "tabla business_visual_identity debe existir (migración 026)"
        cols = {r[1] for r in c.execute("PRAGMA table_info(business_visual_identity)")}
        assert {"id", "business_id", "config", "locked_options", "updated_at"} <= cols
        idx = c.execute("PRAGMA index_list(business_visual_identity)").fetchall()
        unique_business = False
        for i in idx:
            info = c.execute(f"PRAGMA index_info({i[1]})").fetchall()
            names = [r[2] for r in info]
            if names == ["business_id"] and i[2] == 1:
                unique_business = True
        assert unique_business, "UNIQUE en business_id requerido"
    finally:
        c.close()


class TestThemesLibrary(unittest.TestCase):
    def test_eight_presets(self):
        self.assertEqual(len(THEME_PRESETS), 8)
        self.assertEqual(len(preset_names()), 8)

    def test_presets_pass_wcag(self):
        for name, cfg in THEME_PRESETS.items():
            for fg, bg, label in (
                ("page_text_color", "page_background_color", "texto"),
                ("button_text_color", "button_bg_color", "botón"),
            ):
                ratio = contrast_ratio(cfg[fg], cfg[bg])
                self.assertGreaterEqual(
                    ratio, 4.5, f"{name}: {label} contraste {ratio:.2f}:1 < 4.5"
                )

    def test_get_preset_fallback(self):
        self.assertEqual(get_preset("noexiste")["theme_preset"], "moderno")
        self.assertEqual(get_preset(None)["theme_preset"], "moderno")

    def test_default_config_complete(self):
        cfg = default_config()
        for key in CONFIG_KEYS:
            self.assertIn(key, cfg)

    def test_normalize_config_discards_unknown_keys(self):
        cfg = normalize_config({"primary_color": "#FF0000", "hacker": "1"})
        self.assertNotIn("hacker", cfg)
        self.assertEqual(cfg["primary_color"], "#FF0000")

    def test_normalize_config_invalid_color_falls_to_base(self):
        cfg = normalize_config({"primary_color": "zzz"}, base=default_config())
        self.assertEqual(cfg["primary_color"], default_config()["primary_color"])

    def test_normalize_config_font_allowlist(self):
        cfg = normalize_config({"font_family": "Comic Sans; evil"})
        self.assertNotIn("evil", cfg["font_family"])
        cfg2 = normalize_config({"font_family": "Poppins"})
        self.assertEqual(cfg2["font_family"], "Poppins")

    def test_lockable_options_subset_of_config(self):
        self.assertTrue(set(LOCKABLE_OPTIONS) <= set(CONFIG_KEYS))


@pytest.mark.usefixtures("sqlite_temp_db")
class TestVisualIdentityService(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.business_id = _provision()

    def test_save_and_load_roundtrip(self):
        result = vi.save_identity(
            self.business_id, {"primary_color": "#AA0000", "secondary_color": "#220000"}
        )
        self.assertTrue(result["success"], result)
        config, locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#AA0000")
        self.assertEqual(config["secondary_color"], "#220000")
        self.assertEqual(locked, [])

    def test_save_mirrors_to_business_settings(self):
        vi.save_identity(
            self.business_id,
            {
                "primary_color": "#0055AA",
                "secondary_color": "#001122",
                "logo_url": "/media/business_1/logo.png",
            },
        )
        c = get_connection()
        try:
            row = c.execute(
                "SELECT primary_color, secondary_color, logo_url FROM business_settings "
                "WHERE business_id = ?",
                (self.business_id,),
            ).fetchone()
        finally:
            c.close()
        self.assertEqual(row["primary_color"], "#0055AA")
        self.assertEqual(row["secondary_color"], "#001122")
        self.assertEqual(row["logo_url"], "/media/business_1/logo.png")

    def test_invalid_color_rejected(self):
        result = vi.save_identity(self.business_id, {"primary_color": "rojo"})
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_config")
        self.assertTrue(any("primary_color" in e for e in result["errors"]))

    def test_bad_font_rejected(self):
        result = vi.save_identity(self.business_id, {"font_family": "<script>"})
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_config")

    def test_bad_media_rejected(self):
        result = vi.save_identity(self.business_id, {"logo_url": "javascript:alert(1)"})
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_config")

    def test_contrast_below_minimum_rejected(self):
        result = vi.save_identity(
            self.business_id, {"page_text_color": "#FFFFFE", "page_background_color": "#FFFFFF"}
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "contrast_below_minimum")
        self.assertTrue(result["violations"])

    def test_contrast_ok_accepted(self):
        result = vi.save_identity(
            self.business_id,
            {
                "page_text_color": "#000000",
                "page_background_color": "#FFFFFF",
                "button_bg_color": "#1463FF",
                "button_text_color": "#FFFFFF",
            },
        )
        self.assertTrue(result["success"], result)

    def test_tenant_isolation(self):
        other_id = _provision("test-vi-biz2", "owner2@test-vi.com")
        vi.save_identity(self.business_id, {"primary_color": "#111111"})
        vi.save_identity(other_id, {"primary_color": "#222222"})
        config_a, _ = vi.load_identity(self.business_id)
        config_b, _ = vi.load_identity(other_id)
        self.assertEqual(config_a["primary_color"], "#111111")
        self.assertEqual(config_b["primary_color"], "#222222")

    def test_locked_options_enforced(self):
        vi.save_identity(self.business_id, {"primary_color": "#111111"})
        lock_result = vi.save_locked_options(self.business_id, ["primary_color"])
        self.assertTrue(lock_result["success"], lock_result)

        # El tenant intenta cambiar un campo bloqueado: se conserva el vigente.
        result = vi.save_identity(self.business_id, {"primary_color": "#FF00FF"})
        self.assertTrue(result["success"], result)
        config, locked = vi.load_identity(self.business_id)
        self.assertEqual(config["primary_color"], "#111111")
        self.assertEqual(locked, ["primary_color"])

    def test_save_locked_options_unknown_rejected(self):
        result = vi.save_locked_options(self.business_id, ["no_existe"])
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unknown_options")

    def test_apply_preset(self):
        result = vi.apply_preset(self.business_id, "elegante")
        self.assertTrue(result["success"], result)
        config, _ = vi.load_identity(self.business_id)
        self.assertEqual(config["theme_preset"], "elegante")
        self.assertEqual(config["primary_color"], THEME_PRESETS["elegante"]["primary_color"])

    def test_apply_unknown_preset_rejected(self):
        result = vi.apply_preset(self.business_id, "noexiste")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unknown_preset")

    def test_get_visual_identity_scoped_returns_row(self):
        vi.save_identity(self.business_id, {"primary_color": "#333333"})
        row = get_visual_identity_scoped(self.business_id)
        self.assertIsNotNone(row)
        stored = json.loads(row["config"])
        self.assertEqual(stored["primary_color"], "#333333")

    def test_effective_public_settings_merges(self):
        vi.save_identity(
            self.business_id, {"primary_color": "#770000", "logo_url": "/media/business_1/l.png"}
        )
        from database.database import get_business_settings_scoped

        settings = get_business_settings_scoped(self.business_id)
        merged = vi.effective_public_settings(self.business_id, settings)
        self.assertEqual(merged["primary_color"], "#770000")
        self.assertEqual(merged["logo_url"], "/media/business_1/l.png")
        self.assertIn("visual_identity", merged)


if __name__ == "__main__":
    unittest.main()
