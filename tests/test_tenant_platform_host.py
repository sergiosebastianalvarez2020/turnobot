"""Pruebas de resolución de host de plataforma (tenant.py).

Cubre el comportamiento de ``load_current_business`` respecto a:

* hosts de plataforma (localhost, 127.0.0.1, dominio canónico PUBLIC_BASE_URL),
* hosts desconocidos (404),
* disponibilidad del endpoint de salud pública (/health) sobre host de plataforma,
* landing pública.

Todas las pruebas corren sobre SQLite (no requieren PostgreSQL).
"""

import unittest

import pytest

import app as application

pytestmark = pytest.mark.no_pg


class BaseTenantHostTest(unittest.TestCase):
    """Cliente de prueba de Flask sobre la aplicación real."""

    def setUp(self):
        application.rate_limit_state.clear()
        self.client = application.app.test_client()
        self._original_public_base_url = application.app.config.get(
            "PUBLIC_BASE_URL", "http://localhost"
        )

    def tearDown(self):
        application.app.config["PUBLIC_BASE_URL"] = self._original_public_base_url

    def _get(self, path, host, scheme="https"):
        """Ejecuta una petición GET simulando el encabezado Host dado."""
        return self.client.get(path, base_url=f"{scheme}://{host}")

    def _set_public_base_url(self, value):
        """Establece PUBLIC_BASE_URL en la configuración de la aplicación."""
        application.app.config["PUBLIC_BASE_URL"] = value


class TestPlatformHosts(BaseTenantHostTest):
    """Los hosts de plataforma deben resolver el negocio por defecto y servir
    la landing de marketing."""

    def test_localhost_is_platform_host(self):
        response = self._get("/", "localhost", scheme="http")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"marketing-theme", response.data)

    def test_127_0_0_1_is_platform_host(self):
        response = self._get("/", "127.0.0.1", scheme="http")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"marketing-theme", response.data)

    def test_turnobot_net_ar_is_platform_host(self):
        self._set_public_base_url("https://turnobot.net.ar")
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"marketing-theme", response.data)

    def test_unknown_host_returns_404(self):
        response = self._get("/", "unknown.example.com", scheme="https")
        self.assertEqual(response.status_code, 404)

    def test_health_local_returns_200(self):
        response = self._get("/health", "localhost", scheme="http")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"status":"ok"', response.data)

    def test_health_public_returns_200_on_platform_host(self):
        self._set_public_base_url("https://turnobot.net.ar")
        response = self._get("/health", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"status":"ok"', response.data)

    def test_health_unknown_host_returns_404(self):
        response = self._get("/health", "unknown.example.com", scheme="https")
        self.assertEqual(response.status_code, 404)

    def test_landing_public_responds(self):
        response = self._get("/", "localhost", scheme="http")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Más tiempo", body)
        self.assertIn("TurnoBot", body)


class TestSecurity(BaseTenantHostTest):
    """Ningún host arbitrario es aceptado; no hay suplantación de tenant."""

    def test_arbitrary_host_is_rejected(self):
        for host in ("evil.com", "turnobot.net.ar.evil.com", "localhost.evil.com"):
            response = self._get("/", host, scheme="https")
            self.assertEqual(response.status_code, 404, f"host {host} no debería resolverse")


class TestConfigurationEdgeCases(BaseTenantHostTest):
    """PUBLIC_BASE_URL con valores inválidos no debe aceptar hosts arbitrarios."""

    def test_public_base_url_absent_turns_unknown_host_to_404(self):
        """Sin PUBLIC_BASE_URL, solo localhost y 127.0.0.1 son hosts de plataforma."""
        self._set_public_base_url("")
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 404)

    def test_public_base_url_malformed_does_not_accept_arbitrary_hosts(self):
        """PUBLIC_BASE_URL malformada no debe hacer que cualquier host sea válido."""
        self._set_public_base_url("not-a-valid-url")
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 404)

    def test_public_base_url_with_port_extracts_hostname(self):
        """Puerto en PUBLIC_BASE_URL no afecta la extracción del hostname."""
        self._set_public_base_url("https://turnobot.net.ar:8443")
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 200)

    def test_public_base_url_with_path_extracts_hostname(self):
        """Ruta adicional en PUBLIC_BASE_URL no afecta el hostname."""
        self._set_public_base_url("https://turnobot.net.ar/app")
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 200)

    def test_public_base_url_uppercase_hostname_is_lowercased(self):
        """Hostname en mayúsculas en PUBLIC_BASE_URL se normaliza a minúsculas."""
        self._set_public_base_url("https://TURNOBOT.NET.AR")
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 200)

    def test_public_base_url_without_scheme_is_treated_as_hostname(self):
        """PUBLIC_BASE_URL sin esquema: urlsplit trata todo como path, hostname vacío."""
        self._set_public_base_url("turnobot.net.ar")
        # urlsplit("turnobot.net.ar") → hostname=None, path="turnobot.net.ar"
        # No se agrega ningún host a platform_hosts
        response = self._get("/", "turnobot.net.ar", scheme="https")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
