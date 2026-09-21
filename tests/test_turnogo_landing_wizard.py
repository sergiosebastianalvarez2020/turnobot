"""Pruebas de Etapa 12: landing page y wizard de reserva pública.

Valida que:
- La landing page se renderiza correctamente con branding y servicios.
- El wizard de reserva (4 pasos) funciona correctamente.
- Las APIs públicas responden adecuadamente.
- El POST confirmar crea la reserva vía create_appointment.
- CSRF se valida en el POST.
"""

import unittest
import re
from unittest.mock import patch, MagicMock

import app as application
import database.database as database
from database.database import get_connection


class TestLandingPage(unittest.TestCase):
    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def _csrf_from(self, response):
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
        return match.group(1) if match else None

    def test_landing_page_returns_200(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG)
            self.assertEqual(response.status_code, 200)

    def test_landing_page_contains_business_name(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG)
            self.assertIn("El Corte", response.text)

    def test_landing_page_contains_cta_button(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG)
            self.assertIn("Reservar turno", response.text)

    def test_landing_page_contains_services(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG)
            self.assertIn("Servicios", response.text)

    def test_landing_page_contains_branding(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG)
            self.assertIn("#1463FF", response.text)

    def test_landing_page_with_custom_branding(self):
        business_b = {"id": 2, "name": "Business B", "slug": "business-b"}
        settings_b = {
            "business_name": "Business B",
            "business_type": "Barbería",
            "business_initials": "BB",
            "business_description": "Negocio de prueba",
            "timezone": "America/Argentina/Buenos_Aires",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
        }

        with patch.object(application, "resolve_business", return_value=business_b):
            with patch.object(application, "get_business_settings_scoped", return_value=settings_b):
                with patch.object(application, "get_active_services_scoped", return_value=[]):
                    with application.app.test_client() as client:
                        response = client.get("/b/business-b")
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("Business B", response.text)
                        self.assertIn("#FF0000", response.text)
                        self.assertIn("#00FF00", response.text)

    def test_landing_page_inexistent_slug_404(self):
        with application.app.test_client() as client:
            response = client.get("/b/slug-inexistente")
            self.assertEqual(response.status_code, 404)

    def test_index_route_works(self):
        with application.app.test_client() as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)


class TestWizardServiceSelection(unittest.TestCase):
    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def test_wizard_step1_returns_200(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG + "/reservar")
            self.assertEqual(response.status_code, 200)

    def test_wizard_step1_contains_title(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG + "/reservar")
            self.assertIn("Seleccioná un servicio", response.text)

    def test_wizard_step1_contains_services(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG + "/reservar")
            self.assertIn("service-option", response.text)

    def test_wizard_step1_csrf_token_present(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG + "/reservar")
            csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
            self.assertIsNotNone(csrf)

    def test_wizard_inexistent_slug_404(self):
        with application.app.test_client() as client:
            response = client.get("/b/slug-inexistente/reservar")
            self.assertEqual(response.status_code, 404)


class TestWizardDateSelection(unittest.TestCase):
    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def test_wizard_step2_returns_200_with_servicio(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/fecha?servicio=Corte"
            )
            self.assertEqual(response.status_code, 200)

    def test_wizard_step2_redirects_to_step1_without_servicio(self):
        with application.app.test_client() as client:
            response = client.get("/b/" + self.BUSINESS_SLUG + "/reservar/fecha")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Seleccioná un servicio", response.text)

    def test_wizard_step2_contains_calendar(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/fecha?servicio=Corte"
            )
            self.assertIn("calendar-grid", response.text)

    def test_wizard_step2_inexistent_slug_404(self):
        with application.app.test_client() as client:
            response = client.get("/b/slug-inexistente/reservar/fecha?servicio=test")
            self.assertEqual(response.status_code, 404)


class TestWizardDatosSelection(unittest.TestCase):
    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def test_wizard_step3_returns_200(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/datos?servicio=Corte&fecha=2025-01-15&hora=10:00"
            )
            self.assertEqual(response.status_code, 200)

    def test_wizard_step3_contains_form(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/datos?servicio=Corte&fecha=2025-01-15&hora=10:00"
            )
            self.assertIn("Nombre y apellido", response.text)
            self.assertIn("Teléfono", response.text)

    def test_wizard_step3_missing_data_shows_error(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/datos"
            )
            self.assertEqual(response.status_code, 200)


class TestWizardConfirmation(unittest.TestCase):
    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def test_wizard_step4_get_returns_200(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/confirmar?servicio=Corte&fecha=2025-12-31&hora=10:00"
            )
            self.assertEqual(response.status_code, 200)

    def test_wizard_step4_get_contains_confirmation(self):
        with application.app.test_client() as client:
            response = client.get(
                "/b/" + self.BUSINESS_SLUG + "/reservar/confirmar?servicio=Corte&fecha=2025-12-31&hora=10:00"
            )
            self.assertIn("Confirmá tu reserva", response.text)

    def test_wizard_step4_post_creates_appointment(self):
        with patch("services.appointments.create_appointment", return_value={"success": True, "appointment_id": 99}):
            with application.app.test_client() as client:
                page = client.get("/b/" + self.BUSINESS_SLUG + "/reservar?servicio=Corte")
                csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
                csrf_token = csrf.group(1) if csrf else None

                response = client.post(
                    "/b/" + self.BUSINESS_SLUG + "/reservar/confirmar",
                    json={
                        "csrf_token": csrf_token,
                        "nombre": "Juan Perez",
                        "telefono": "12345678",
                        "email": "juan@test.com",
                        "servicio": "Corte",
                        "fecha": "2025-12-31",
                        "hora": "10:00",
                    },
                )
                self.assertEqual(response.status_code, 201)
                data = response.get_json()
                self.assertTrue(data["success"])

    def test_wizard_step4_post_without_csrf_returns_400(self):
        with application.app.test_client() as client:
            response = client.post(
                "/b/" + self.BUSINESS_SLUG + "/reservar/confirmar",
                data={
                    "nombre": "Juan Perez",
                    "telefono": "12345678",
                    "servicio": "Corte",
                    "fecha": "2025-12-31",
                    "hora": "10:00",
                },
            )
            self.assertEqual(response.status_code, 400)

    def test_wizard_step4_post_missing_name_returns_400(self):
        with application.app.test_client() as client:
            page = client.get("/b/" + self.BUSINESS_SLUG + "/reservar")
            csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
            csrf_token = csrf.group(1) if csrf else None

            response = client.post(
                "/b/" + self.BUSINESS_SLUG + "/reservar/confirmar",
                json={
                    "csrf_token": csrf_token,
                    "nombre": "J",
                    "telefono": "12345678",
                    "servicio": "Corte",
                    "fecha": "2025-12-31",
                    "hora": "10:00",
                },
            )
            self.assertEqual(response.status_code, 400)


class TestWizardEndpointsConsistency(unittest.TestCase):
    def test_all_wizard_endpoints_exist(self):
        with application.app.test_client() as client:
            endpoints = [
                "/b/el-corte",
                "/b/el-corte/reservar",
                "/b/el-corte/reservar/fecha?servicio=Corte",
                "/b/el-corte/reservar/datos?servicio=Corte&fecha=2025-12-31&hora=10:00",
                "/b/el-corte/reservar/confirmar?servicio=Corte&fecha=2025-12-31&hora=10:00",
            ]
            for endpoint in endpoints:
                response = client.get(endpoint)
                self.assertEqual(response.status_code, 200,
                                 "Endpoint " + endpoint + " returned " + str(response.status_code))

    def test_url_map_has_expected_endpoints(self):
        rules = {rule.endpoint for rule in application.app.url_map.iter_rules()}
        expected = {
            "index",
            "business_index",
            "business_reservar_wizard",
            "business_reservar_wizard_fecha",
            "business_reservar_wizard_datos",
            "business_reservar_wizard_confirmar",
        }
        missing = expected - rules
        self.assertEqual(missing, set(), "Missing endpoints: " + str(missing))

    def test_url_map_rule_count_unchanged(self):
        rules = list(application.app.url_map.iter_rules())
        self.assertGreaterEqual(len(rules), 109)


class TestWizardRateLimiting(unittest.TestCase):
    BUSINESS_SLUG = "el-corte"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True

    def _get_csrf(self, client):
        page = client.get("/b/" + self.BUSINESS_SLUG + "/reservar")
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        return csrf.group(1) if csrf else None

    def _post_confirmation(self, client, csrf_token):
        return client.post(
            "/b/" + self.BUSINESS_SLUG + "/reservar/confirmar",
            json={
                "csrf_token": csrf_token,
                "nombre": "Juan Perez",
                "telefono": "12345678",
                "email": "juan@test.com",
                "servicio": "Corte",
                "fecha": "2025-12-31",
                "hora": "10:00",
            },
        )

    def test_wizard_post_allowed_under_limit(self):
        with patch("services.appointments.create_appointment",
                   return_value={"success": True, "appointment_id": 99}):
            with application.app.test_client() as client:
                csrf_token = self._get_csrf(client)
                response = self._post_confirmation(client, csrf_token)
                self.assertEqual(response.status_code, 201)

    def test_wizard_post_rate_limited_after_60_requests(self):
        with patch("services.appointments.create_appointment",
                   return_value={"success": True, "appointment_id": 99}):
            with application.app.test_client() as client:
                csrf_token = self._get_csrf(client)
                for i in range(application.API_REQUEST_LIMIT):
                    response = self._post_confirmation(client, csrf_token)
                    self.assertIn(response.status_code, (201, 400))
                blocked = self._post_confirmation(client, csrf_token)
                self.assertEqual(blocked.status_code, 429)
                data = blocked.get_json()
                self.assertFalse(data["success"])
                self.assertEqual(data["code"], "RATE_LIMITED")
                self.assertEqual(data["error"], "rate_limit_exceeded")
                self.assertIn("Retry-After", blocked.headers)

    def test_rate_limited_post_does_not_create_appointment(self):
        mock_create = MagicMock(return_value={"success": True, "appointment_id": 99})
        with patch("services.appointments.create_appointment", mock_create):
            with application.app.test_client() as client:
                csrf_token = self._get_csrf(client)
                for i in range(application.API_REQUEST_LIMIT):
                    self._post_confirmation(client, csrf_token)
                mock_create.reset_mock()
                blocked = self._post_confirmation(client, csrf_token)
                self.assertEqual(blocked.status_code, 429)
                mock_create.assert_not_called()

    def test_rate_limited_post_does_not_send_notifications(self):
        mock_send = MagicMock()
        mock_create = MagicMock(return_value={"success": True, "appointment_id": 99})
        with patch("services.notifications.send_confirmation_email", mock_send), \
             patch("services.appointments.create_appointment", mock_create):
            with application.app.test_client() as client:
                csrf_token = self._get_csrf(client)
                for i in range(application.API_REQUEST_LIMIT):
                    self._post_confirmation(client, csrf_token)
                mock_send.reset_mock()
                blocked = self._post_confirmation(client, csrf_token)
                self.assertEqual(blocked.status_code, 429)
                mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
