"""Etapa 9: accesibilidad y UX en el panel administrativo.

Cubre:
- Skip link (Saltar al contenido) y target main-content.
- ARIA live/alert en feedback dinámico del admin.
- aria-label en controles de acción por turno y por miembro.
- aria-hidden en emojis decorativos (nav, stat-icons, empty-icon).
- Foco visible (focus-visible) en CSS (admin.css, style.css).
- prefers-reduced-motion en CSS.
- Contraste de token --text-muted mejorado.
"""

import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection
from services import appointments


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class AccessibilityUITest(unittest.TestCase):

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self.client = application.app.test_client()
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

        login_page = self.client.get("/login")
        self.csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"', login_page.text
        ).group(1)
        self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": self.csrf_token},
        )

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _create_turno(self, date_=None, time_="09:00"):
        date_ = date_ or _next_open_day()
        result = appointments.create_appointment(
            "Carlos López", "3838439000", "Corte", date_, time_, 1
        )
        self.assertTrue(result["success"])
        return result["appointment_id"], date_

    # --- Skip link ---

    def test_admin_has_skip_link(self):
        page = self.client.get("/admin")
        self.assertIn("Saltar al contenido", page.text)
        self.assertIn('href="#main-content"', page.text)

    def test_admin_main_has_id(self):
        page = self.client.get("/admin")
        self.assertIn('id="main-content"', page.text)

    # --- ARIA roles on feedback ---

    def test_reschedule_error_uses_role_alert(self):
        appt_id, date_ = self._create_turno()
        page = self.client.post(
            f"/admin/turnos/{appt_id}/reprogramar",
            data={
                "csrf_token": self.csrf_token,
                "new_date": "",
                "new_time": "",
            },
            follow_redirects=True,
        )
        self.assertIn('role="alert"', page.text)

    # --- aria-label on per-appointment actions ---

    def test_admin_action_controls_have_aria_labels(self):
        appt_id, date_ = self._create_turno()
        page = self.client.get("/admin")
        self.assertIn(
            f'aria-label="Nuevo estado para Carlos L', page.text
        )
        self.assertIn(
            f'aria-label="Aplicar nuevo estado a Carlos L', page.text
        )
        self.assertIn(
            f'aria-label="Fecha de reprogramación para Carlos L', page.text
        )
        self.assertIn(
            f'aria-label="Hora de reprogramación para Carlos L', page.text
        )
        self.assertIn(
            f'aria-label="Reprogramar turno de Carlos L', page.text
        )

    # --- aria-hidden on decorative emojis ---

    def test_admin_nav_emojis_are_hidden(self):
        page = self.client.get("/admin")
        nav_section = re.search(r"<nav>(.*?)</nav>", page.text, re.DOTALL)
        self.assertIsNotNone(nav_section)
        aria_hidden_count = nav_section.group(0).count('aria-hidden="true"')
        self.assertGreaterEqual(aria_hidden_count, 4)

    def test_admin_stat_icons_are_hidden(self):
        page = self.client.get("/admin")
        stat_icons = re.findall(r'class="stat-icon"', page.text)
        self.assertGreaterEqual(len(stat_icons), 6)
        hidden_icons = page.text.count('aria-hidden="true"')
        self.assertGreaterEqual(hidden_icons, 6)

    # --- CSS: focus-visible ---

    def test_admin_css_has_focus_visible(self):
        css_path = Path(__file__).resolve().parent.parent / "static" / "admin.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)

    def test_style_css_has_focus_visible(self):
        css_path = Path(__file__).resolve().parent.parent / "static" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)

    # --- CSS: prefers-reduced-motion ---

    def test_admin_css_has_reduced_motion(self):
        css_path = Path(__file__).resolve().parent.parent / "static" / "admin.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)

    def test_style_css_has_reduced_motion(self):
        css_path = Path(__file__).resolve().parent.parent / "static" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)

    # --- Contrast token ---

    def test_style_css_muted_text_has_improved_contrast(self):
        css_path = Path(__file__).resolve().parent.parent / "static" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertNotIn("--text-muted: #8DA4C8", css)
        self.assertIn("--text-muted: #5B6B84", css)

    def test_admin_css_no_low_contrast_auxiliary(self):
        css_path = Path(__file__).resolve().parent.parent / "static" / "admin.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertNotIn("#8DA4C8", css)

    # --- Login error role ---

    def test_login_error_has_role_alert(self):
        page = self.client.post(
            "/login",
            data={"password": "mala", "csrf_token": self.csrf_token},
        )
        self.assertIn('role="alert"', page.text)

    # --- Login contrast ---

    def test_login_page_no_low_contrast_colors(self):
        # Cliente nuevo: el client del setUp ya tiene sesión y /login redirige.
        fresh_client = application.app.test_client()
        page = fresh_client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("#8DA4C8", page.text)
        self.assertIn("#5B6B84", page.text)

    # --- Public manage page: status label coherence ---

    def test_manage_turno_status_label_is_spanish(self):
        result = appointments.create_appointment(
            "Laura Gómez", "3838439001", "Corte", _next_open_day(), "09:00", 1
        )
        self.assertTrue(result["success"])
        page = self.client.get(
            "/b/el-corte/turno/{}?id={}".format(
                result["management_token"], result["appointment_id"]
            )
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("Confirmado", page.text)
        self.assertNotIn(">confirmed</span>", page.text)

    # --- Usuarios page accessibility ---

    def test_usuarios_skip_link(self):
        page = self.client.get("/admin/usuarios")
        self.assertIn("Saltar al contenido", page.text)
        self.assertIn('href="#main-content"', page.text)
        self.assertIn('id="main-content"', page.text)

    def test_usuarios_nav_emojis_are_hidden(self):
        page = self.client.get("/admin/usuarios")
        nav_section = re.search(r"<nav>(.*?)</nav>", page.text, re.DOTALL)
        self.assertIsNotNone(nav_section)
        aria_hidden_count = nav_section.group(0).count('aria-hidden="true"')
        self.assertGreaterEqual(aria_hidden_count, 4)


if __name__ == "__main__":
    unittest.main()
