"""Pruebas de Etapa 11: wizard de onboarding.

Valida que un owner nuevo ve un checklist granular que refleja el estado
real de configuración del negocio, derivado de las tablas existentes.
"""

import tempfile
import unittest
from pathlib import Path

import app as application
import database.database as database
from database.database import get_connection
from services import platform as platform_service
from services import product


class OnboardingBase(unittest.TestCase):
    OWNER_EMAIL = "owner@test-onboarding.com"
    OWNER_PASSWORD = "clave-segura-123"

    def setUp(self):
        application.rate_limit_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "onboarding.db"
        database.init_database()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_db
        self.tmp.cleanup()

    def _query(self, sql, params=None):
        c = get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    def _provision_and_login(self):
        result = platform_service.provision_business(
            "Onboarding Biz", "onboarding-biz", self.OWNER_EMAIL
        )
        approved = platform_service.approve_business(result["business_id"])
        token = approved["invitation_url"].rsplit("/", 1)[-1]
        platform_service.accept_invitation(result["business_id"], token, self.OWNER_PASSWORD)

        page = self.client.get("/b/onboarding-biz/login")
        import re

        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        csrf = match.group(1) if match else None
        self.client.post(
            "/b/onboarding-biz/login",
            data={"email": self.OWNER_EMAIL, "password": self.OWNER_PASSWORD, "csrf_token": csrf},
        )
        return result

    def _business_id(self):
        row = self._query("SELECT id FROM businesses WHERE slug = 'onboarding-biz'")
        return row[0]["id"] if row else None


class TestOnboardingState(OnboardingBase):
    def test_new_business_has_partial_steps_completed(self):
        self._provision_and_login()
        settings = database.get_business_settings_scoped(self._business_id())
        services = database.get_all_services_scoped(self._business_id())
        state = product.get_onboarding_state(self._business_id(), settings, services)

        self.assertFalse(state["all_completed"])
        self.assertEqual(state["completed_count"], 2)
        self.assertEqual(state["total_steps"], 6)

        step_keys = [s["key"] for s in state["steps"]]
        self.assertIn("business_data", step_keys)
        self.assertIn("services", step_keys)
        self.assertIn("schedules", step_keys)
        self.assertIn("resources", step_keys)
        self.assertIn("knowledge", step_keys)
        self.assertIn("smtp", step_keys)

    def test_business_data_step_tracks_settings(self):
        self._provision_and_login()
        business_id = self._business_id()

        settings = database.get_business_settings_scoped(business_id)
        services = database.get_all_services_scoped(business_id)
        state = product.get_onboarding_state(business_id, settings, services)

        business_step = next(s for s in state["steps"] if s["key"] == "business_data")
        self.assertTrue(business_step["completed"])

        database.update_business_settings_scoped(business_id, "", "", "", "", "UTC")
        settings = database.get_business_settings_scoped(business_id)
        state = product.get_onboarding_state(business_id, settings, services)
        business_step = next(s for s in state["steps"] if s["key"] == "business_data")
        self.assertFalse(business_step["completed"])

    def test_services_step_tracks_services(self):
        self._provision_and_login()
        business_id = self._business_id()

        settings = database.get_business_settings_scoped(business_id)
        services = database.get_all_services_scoped(business_id)
        state = product.get_onboarding_state(business_id, settings, services)
        services_step = next(s for s in state["steps"] if s["key"] == "services")
        self.assertFalse(services_step["completed"])

        database.create_service_scoped(business_id, "Corte", 1000, 30, True)
        services = database.get_all_services_scoped(business_id)
        state = product.get_onboarding_state(business_id, settings, services)
        services_step = next(s for s in state["steps"] if s["key"] == "services")
        self.assertTrue(services_step["completed"])

    def test_schedules_step_tracks_open_days(self):
        self._provision_and_login()
        business_id = self._business_id()

        settings = database.get_business_settings_scoped(business_id)
        services = database.get_all_services_scoped(business_id)
        state = product.get_onboarding_state(business_id, settings, services)
        schedules_step = next(s for s in state["steps"] if s["key"] == "schedules")
        self.assertTrue(schedules_step["completed"])

        for day in range(7):
            database.update_weekly_schedule_scoped(
                business_id,
                day,
                is_open=False,
                morning_start="",
                morning_end="",
                afternoon_start="",
                afternoon_end="",
            )
        state = product.get_onboarding_state(business_id, settings, services)
        schedules_step = next(s for s in state["steps"] if s["key"] == "schedules")
        self.assertFalse(schedules_step["completed"])

    def test_resources_step_is_skippable(self):
        self._provision_and_login()
        business_id = self._business_id()

        settings = database.get_business_settings_scoped(business_id)
        services = database.get_all_services_scoped(business_id)
        state = product.get_onboarding_state(business_id, settings, services)
        resources_step = next(s for s in state["steps"] if s["key"] == "resources")
        self.assertTrue(resources_step.get("skippable"))

    def test_all_completed_when_fully_configured(self):
        self._provision_and_login()
        business_id = self._business_id()

        database.update_business_settings_scoped(
            business_id, "Test Biz", "Barbería", "TB", "", "UTC"
        )
        database.create_service_scoped(business_id, "Corte", 1000, 30, True)
        database.update_weekly_schedule_scoped(
            business_id,
            0,
            is_open=True,
            morning_start="09:00",
            morning_end="12:00",
            afternoon_start="14:00",
            afternoon_end="18:00",
        )
        from services.knowledge import create_knowledge_scoped

        create_knowledge_scoped(
            business_id, "faq", "¿Qué horarios tenés?", "De lunes a viernes.", "horarios", 1
        )

        import os

        os.environ["SMTP_HOST"] = "smtp.test.com"
        try:
            settings = database.get_business_settings_scoped(business_id)
            services = database.get_all_services_scoped(business_id)
            state = product.get_onboarding_state(business_id, settings, services)
            self.assertTrue(state["all_completed"])
        finally:
            del os.environ["SMTP_HOST"]

    def test_onboarding_checklist_visible_in_admin(self):
        self._provision_and_login()
        page = self.client.get("/b/onboarding-biz/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Prepará tu negocio", page.text)
        self.assertIn("Datos del negocio", page.text)
        self.assertIn("Servicios", page.text)
        self.assertIn("Horarios semanales", page.text)
        self.assertIn("Recursos", page.text)
        self.assertIn("Información para la IA", page.text)
        self.assertIn("Notificaciones", page.text)


if __name__ == "__main__":
    unittest.main()
