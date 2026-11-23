"""Tests de protección SSRF del proxy de imágenes externas.

Cubre:
- Validación de esquema, host y credenciales.
- Bloqueo de IPs privadas (loopback, RFC 1918, link-local, metadata cloud).
- Bloqueo de redirecciones hacia IPs internas (301/302/307/308).
- Rechazo de redirecciones (allow_redirects=False garantizado).
- Sin segunda solicitud HTTP tras rechazar una redirección.
- DNS rebinding: host que resuelve a IP privada → bloqueado.
- Host con múltiples IPs donde una es privada → bloqueado.
- Error DNS → bloqueado.
- URL con credenciales → rechazada antes de cualquier conexión.
- Esquema no permitido → rechazado.
- Respuesta que no es imagen → rechazada.
- Imagen que excede el tamaño máximo → rechazada.
- Error de conexión y timeout → rechazados.
- Respuestas cerradas en todos los caminos (incluyendo rechazos).
- Respuesta 200 con imagen válida → datos retornados.
"""

from __future__ import annotations

import socket
import unittest
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import ConnectionError, Timeout

pytestmark = pytest.mark.no_pg

# PNG válido mínimo (1×1 pixel).
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_response(
    status_code: int = 200,
    content_type: str = "image/png",
    body: bytes = _TINY_PNG,
    headers: dict | None = None,
) -> MagicMock:
    """Crea un mock de requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_redirect = status_code in {301, 302, 303, 307, 308}
    all_headers: dict[str, str] = {"Content-Type": content_type}
    if headers:
        all_headers.update(headers)
    resp.headers = all_headers
    resp.close = MagicMock()

    # iter_content devuelve el cuerpo en un único chunk.
    resp.iter_content = lambda chunk_size=8192: iter([body]) if body else iter([])
    return resp


class TestFetchExternalImageInputValidation(unittest.TestCase):
    """Validaciones que se resuelven sin ninguna llamada de red."""

    def _fetch(self, url):
        from application.media import fetch_external_image

        return fetch_external_image(url)

    def test_empty_url_rejected(self):
        data, error = self._fetch("")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_blank_url_rejected(self):
        data, error = self._fetch("   ")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_scheme_ftp_rejected(self):
        """Esquema ftp no permitido."""
        data, error = self._fetch("ftp://example.com/img.png")
        self.assertIsNone(data)
        self.assertIn("http", error.lower())

    def test_scheme_file_rejected(self):
        """Esquema file:// no permitido."""
        data, error = self._fetch("file:///etc/passwd")
        self.assertIsNone(data)
        self.assertIn("http", error.lower())

    def test_url_with_credentials_rejected(self):
        """URL con usuario:contraseña rechazada antes de conectar."""
        data, error = self._fetch("http://user:secret@example.com/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_url_with_username_only_rejected(self):
        """URL con usuario sin contraseña rechazada."""
        data, error = self._fetch("http://user@example.com/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_private_ip_loopback_rejected(self):
        """IP de loopback bloqueada en la etapa de validación del host."""
        data, error = self._fetch("http://127.0.0.1/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_private_ip_rfc1918_10_rejected(self):
        """Red 10.0.0.0/8 bloqueada."""
        data, error = self._fetch("http://10.0.0.1/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_private_ip_rfc1918_192_rejected(self):
        """Red 192.168.x.x bloqueada."""
        data, error = self._fetch("http://192.168.1.1/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_private_ip_link_local_metadata_rejected(self):
        """169.254.169.254 (metadata cloud) bloqueada."""
        data, error = self._fetch("http://169.254.169.254/latest/meta-data/")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_private_ipv6_loopback_rejected(self):
        """::1 (loopback IPv6) bloqueada."""
        data, error = self._fetch("http://[::1]/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)


class TestFetchExternalImageDNSResolution(unittest.TestCase):
    """Tests de resolución DNS con mocks de socket.getaddrinfo."""

    def _fetch(self, url):
        from application.media import fetch_external_image

        return fetch_external_image(url)

    def _make_addrinfo(self, ip: str):
        """Construye una respuesta de getaddrinfo mínima para una IP."""
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, "", (ip, 0))]

    def test_host_resolving_to_private_ip_blocked(self):
        """Host que resuelve a IP privada → bloqueado (DNS rebinding mitigation)."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("10.0.0.1")):
            data, error = self._fetch("http://evil.example.com/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_host_with_multiple_ips_one_private_blocked(self):
        """Host con varias IPs donde una es privada → bloqueado (fail-secure)."""
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("5.6.7.8", 0)),
        ]
        with patch("socket.getaddrinfo", return_value=infos):
            data, error = self._fetch("http://mixed.example.com/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_dns_error_blocked(self):
        """Error de resolución DNS → bloqueado."""
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
            data, error = self._fetch("http://nonexistent.example.com/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_host_resolving_to_link_local_blocked(self):
        """Host que resuelve a 169.254.x.x (link-local) → bloqueado."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("169.254.169.254")):
            data, error = self._fetch("http://metadata.internal.example/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_host_resolving_to_ipv6_private_blocked(self):
        """Host que resuelve a fc00::/7 (IPv6 unique-local) → bloqueado."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("fc00::1")):
            data, error = self._fetch("http://ipv6private.example.com/img.png")
        self.assertIsNone(data)
        self.assertIsNotNone(error)


class TestFetchExternalImageRedirects(unittest.TestCase):
    """Tests de que redirecciones no se siguen (allow_redirects=False).

    Se parchea session.get para verificar:
    1. Que se llama exactamente UNA vez (no se sigue la redirección).
    2. Que la response se cierra aunque sea una redirección.
    3. Que el resultado es un error, no los bytes del destino de la redirección.
    """

    def _fetch_with_mock_session(self, url, mock_response):
        """Parchea session.get y ejecuta fetch_external_image."""
        from application.media import fetch_external_image

        # Mockear _resolve_and_validate_host para devolver una IP pública.
        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", return_value=mock_response) as mock_get:
                result = fetch_external_image(url)
                return result, mock_get

    def test_redirect_301_to_loopback_blocked(self):
        """301 → 127.0.0.1 rechazado; solo UNA petición HTTP."""
        resp = _make_response(
            status_code=301, headers={"Location": "http://127.0.0.1/secret"}, body=b""
        )
        (data, error), mock_get = self._fetch_with_mock_session(
            "http://public.example.com/img.png", resp
        )
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        # Solo una petición: la redirección NO se siguió.
        mock_get.assert_called_once()
        # La respuesta se cerró.
        resp.close.assert_called()

    def test_redirect_302_to_private_network_blocked(self):
        """302 → 10.0.0.1 rechazado."""
        resp = _make_response(
            status_code=302, headers={"Location": "http://10.0.0.1/data"}, body=b""
        )
        (data, error), mock_get = self._fetch_with_mock_session(
            "http://public.example.com/img.png", resp
        )
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        mock_get.assert_called_once()
        resp.close.assert_called()

    def test_redirect_307_to_link_local_blocked(self):
        """307 → 169.254.169.254 rechazado."""
        resp = _make_response(
            status_code=307,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            body=b"",
        )
        (data, error), mock_get = self._fetch_with_mock_session(
            "http://public.example.com/img.png", resp
        )
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        mock_get.assert_called_once()
        resp.close.assert_called()

    def test_redirect_308_to_ipv6_private_blocked(self):
        """308 → dirección IPv6 ULA rechazado."""
        resp = _make_response(
            status_code=308, headers={"Location": "http://[fc00::1]/internal"}, body=b""
        )
        (data, error), mock_get = self._fetch_with_mock_session(
            "http://public.example.com/img.png", resp
        )
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        mock_get.assert_called_once()
        resp.close.assert_called()

    def test_no_second_request_on_redirect(self):
        """Verificar que no se realiza una segunda solicitud HTTP al seguir una redirección."""
        resp = _make_response(
            status_code=302, headers={"Location": "http://127.0.0.1/secret"}, body=b""
        )
        (data, error), mock_get = self._fetch_with_mock_session(
            "http://public.example.com/img.png", resp
        )
        # Exactamente una sola llamada GET; la redirección NO se siguió.
        self.assertEqual(mock_get.call_count, 1)
        self.assertIsNone(data)


class TestFetchExternalImageResponseValidation(unittest.TestCase):
    """Tests de validación de la respuesta HTTP (content-type, tamaño, estado)."""

    def _fetch_with_response(self, url: str, response: MagicMock):
        from application.media import fetch_external_image

        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", return_value=response):
                return fetch_external_image(url)

    def test_200_valid_image_returns_bytes(self):
        """URL pública con respuesta 200 e imagen PNG válida → bytes retornados."""
        resp = _make_response(status_code=200, content_type="image/png", body=_TINY_PNG)
        data, error = self._fetch_with_response("http://cdn.example.com/logo.png", resp)
        self.assertIsNone(error)
        self.assertEqual(data, _TINY_PNG)
        resp.close.assert_called()

    def test_non_image_content_type_rejected(self):
        """Content-Type text/html → rechazado."""
        resp = _make_response(status_code=200, content_type="text/html", body=b"<html></html>")
        data, error = self._fetch_with_response("http://cdn.example.com/page.html", resp)
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        resp.close.assert_called()

    def test_application_json_content_type_rejected(self):
        """Content-Type application/json → rechazado."""
        resp = _make_response(
            status_code=200, content_type="application/json", body=b'{"key":"val"}'
        )
        data, error = self._fetch_with_response("http://cdn.example.com/api.json", resp)
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        resp.close.assert_called()

    def test_image_exceeding_max_size_rejected(self):
        """Imagen que excede el límite de 512 KB → rechazada."""
        from application.media import _EXTERNAL_PROXY_MAX_BYTES

        oversized_body = b"X" * (_EXTERNAL_PROXY_MAX_BYTES + 1)
        resp = _make_response(status_code=200, content_type="image/jpeg", body=oversized_body)
        data, error = self._fetch_with_response("http://cdn.example.com/huge.jpg", resp)
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        resp.close.assert_called()

    def test_non_200_status_rejected(self):
        """Respuesta 404 → rechazada."""
        resp = _make_response(status_code=404, body=b"Not Found")
        data, error = self._fetch_with_response("http://cdn.example.com/missing.png", resp)
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        resp.close.assert_called()

    def test_response_closed_on_rejection(self):
        """Verificar que response.close() se llama incluso al rechazar una respuesta."""
        resp = _make_response(status_code=200, content_type="text/plain", body=b"not an image")
        data, error = self._fetch_with_response("http://cdn.example.com/text.txt", resp)
        self.assertIsNone(data)
        # La respuesta debe estar cerrada aunque sea rechazada.
        resp.close.assert_called()


class TestFetchExternalImageNetworkErrors(unittest.TestCase):
    """Tests de errores de red (ConnectionError, Timeout)."""

    def _fetch_with_error(self, url: str, exception):
        from application.media import fetch_external_image

        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", side_effect=exception):
                return fetch_external_image(url)

    def test_connection_error_rejected(self):
        """Error de conexión TCP → rechazado con mensaje apropiado."""
        data, error = self._fetch_with_error(
            "http://cdn.example.com/img.png", ConnectionError("Connection refused")
        )
        self.assertIsNone(data)
        self.assertIsNotNone(error)

    def test_timeout_rejected(self):
        """Timeout → rechazado."""
        data, error = self._fetch_with_error(
            "http://cdn.example.com/img.png", Timeout("Read timeout")
        )
        self.assertIsNone(data)
        self.assertIsNotNone(error)


class TestIsPrivateHost(unittest.TestCase):
    """Tests unitarios de is_private_host."""

    def _check(self, hostname):
        from application.media import is_private_host

        return is_private_host(hostname)

    def test_loopback_ipv4(self):
        self.assertTrue(self._check("127.0.0.1"))

    def test_rfc1918_10(self):
        self.assertTrue(self._check("10.0.0.1"))

    def test_rfc1918_172(self):
        self.assertTrue(self._check("172.16.0.1"))

    def test_rfc1918_192(self):
        self.assertTrue(self._check("192.168.0.1"))

    def test_link_local_metadata(self):
        self.assertTrue(self._check("169.254.169.254"))

    def test_loopback_ipv6(self):
        self.assertTrue(self._check("::1"))

    def test_ipv4_mapped_ipv6_loopback(self):
        """::ffff:127.0.0.1 debe ser bloqueado (Python 3.11+)."""
        self.assertTrue(self._check("::ffff:127.0.0.1"))

    def test_ipv4_mapped_ipv6_private(self):
        """::ffff:10.0.0.1 debe ser bloqueado."""
        self.assertTrue(self._check("::ffff:10.0.0.1"))

    def test_public_ip_allowed(self):
        self.assertFalse(self._check("8.8.8.8"))

    def test_empty_host_blocked(self):
        self.assertTrue(self._check(""))

    def test_dns_resolves_to_private_blocked(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.1.2.3", 0))]
        with patch("socket.getaddrinfo", return_value=infos):
            self.assertTrue(self._check("internal.company.example"))

    def test_dns_error_blocked(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")):
            self.assertTrue(self._check("nxdomain.example"))

    def test_multiple_ips_one_private_blocked(self):
        """Fail-secure: si alguna IP es privada, todo el host es bloqueado."""
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("socket.getaddrinfo", return_value=infos):
            self.assertTrue(self._check("mixed.example.com"))

    def test_cgnat_rfc6598_low_blocked(self):
        """100.64.0.1 (CGNAT RFC 6598) debe ser bloqueado (not is_global)."""
        self.assertTrue(self._check("100.64.0.1"))

    def test_cgnat_rfc6598_high_blocked(self):
        """100.127.255.254 (tope del bloque CGNAT) debe ser bloqueado."""
        self.assertTrue(self._check("100.127.255.254"))

    def test_cgnat_host_resolves_to_cgnat_blocked(self):
        """Host que resuelve a IP CGNAT (100.64.x.x) → bloqueado."""
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("100.64.1.1", 0))]
        with patch("socket.getaddrinfo", return_value=infos):
            self.assertTrue(self._check("cgnat.example.com"))

    def test_public_global_ip_allowed(self):
        """1.1.1.1 es is_global=True → permitida (no es privada ni CGNAT)."""
        self.assertFalse(self._check("1.1.1.1"))


class TestResolveAndValidateHost(unittest.TestCase):
    """Tests de _resolve_and_validate_host con mocks de socket.getaddrinfo."""

    def _resolve(self, hostname):
        from application.media import _resolve_and_validate_host

        return _resolve_and_validate_host(hostname)

    def _make_addrinfo(self, ip: str):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, "", (ip, 0))]

    def test_public_ip_resolves_ok(self):
        """Host que resuelve a IP pública → lista no vacía, sin error."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("1.2.3.4")):
            results, error = self._resolve("cdn.example.com")
        self.assertIsNone(error)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0][1], "1.2.3.4")

    def test_private_ip_returns_error(self):
        """Host que resuelve a IP privada → error."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("10.0.0.1")):
            results, error = self._resolve("internal.example.com")
        self.assertEqual(results, [])
        self.assertIsNotNone(error)

    def test_dns_gaierror_returns_error(self):
        """Error DNS → error."""
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")):
            results, error = self._resolve("nxdomain.example.com")
        self.assertEqual(results, [])
        self.assertIsNotNone(error)

    def test_empty_addrinfo_returns_error(self):
        """getaddrinfo devuelve lista vacía → error."""
        with patch("socket.getaddrinfo", return_value=[]):
            results, error = self._resolve("weird.example.com")
        self.assertEqual(results, [])
        self.assertIsNotNone(error)

    def test_cgnat_ip_returns_error(self):
        """Host que resuelve a 100.64.0.1 (CGNAT) → error (no es global)."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("100.64.0.1")):
            results, error = self._resolve("cgnat.carrier.example")
        self.assertEqual(results, [])
        self.assertIsNotNone(error)

    def test_cgnat_ip_high_returns_error(self):
        """100.127.255.254 (tope del bloque CGNAT RFC 6598) → error."""
        with patch("socket.getaddrinfo", return_value=self._make_addrinfo("100.127.255.254")):
            results, error = self._resolve("cgnat-top.carrier.example")
        self.assertEqual(results, [])
        self.assertIsNotNone(error)


class TestIsPrivateHostEdgeCases(unittest.TestCase):
    """Casos borde adicionales de is_private_host."""

    def _check(self, hostname, infos=None):
        from application.media import is_private_host

        if infos is not None:
            with patch("socket.getaddrinfo", return_value=infos):
                return is_private_host(hostname)
        return is_private_host(hostname)

    def test_all_public_ips_allowed(self):
        """Host con múltiples IPs todas públicas → permitido."""
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("5.6.7.8", 0)),
        ]
        self.assertFalse(self._check("cdn.example.com", infos=infos))

    def test_empty_addrinfo_blocked(self):
        """getaddrinfo devuelve lista vacía → bloqueado."""
        self.assertTrue(self._check("empty.example.com", infos=[]))

    def test_ipv4_mapped_cgnat_blocked(self):
        """::ffff:100.64.0.1 (IPv4-mapped IPv6 en rango CGNAT) → bloqueado."""
        # is_private devuelve False para esta dirección en Python 3.13;
        # la nueva política (not is_global) la bloquea correctamente.
        self.assertTrue(self._check("::ffff:100.64.0.1"))


class TestFetchExternalImageFullFlow(unittest.TestCase):
    """Tests de flujo completo de fetch_external_image con imagen válida."""

    def test_200_valid_png_full_pipeline(self):
        """Flujo completo: resolve OK, GET OK 200, imagen PNG → retorna bytes."""
        from application.media import fetch_external_image

        resp = _make_response(status_code=200, content_type="image/png", body=_TINY_PNG)

        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", return_value=resp) as mock_get:
                data, error = fetch_external_image("http://cdn.example.com/logo.png")

        self.assertIsNone(error)
        self.assertEqual(data, _TINY_PNG)
        # Solo se hizo una petición GET.
        mock_get.assert_called_once()
        # allow_redirects=False fue pasado.
        call_kwargs = mock_get.call_args[1]
        self.assertFalse(call_kwargs.get("allow_redirects", True))
        resp.close.assert_called()

    def test_allow_redirects_false_always_set(self):
        """Verificar que allow_redirects=False se pasa siempre a requests.get."""
        from application.media import fetch_external_image

        resp = _make_response(status_code=200, content_type="image/png", body=_TINY_PNG)

        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", return_value=resp) as mock_get:
                fetch_external_image("https://cdn.example.com/logo.png")

        call_kwargs = mock_get.call_args[1]
        self.assertIn("allow_redirects", call_kwargs)
        self.assertFalse(call_kwargs["allow_redirects"])


class TestProxyBypass(unittest.TestCase):
    """Tests de que proxies del entorno no pueden desviar la petición SSRF."""

    _TINY_PNG = _TINY_PNG

    def _fetch_with_mocked_net(self, url: str, response: MagicMock):
        from application.media import fetch_external_image

        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", return_value=response) as mock_get:
                result = fetch_external_image(url)
                return result, mock_get

    def test_trust_env_false_in_session(self):
        """La sesión SSRF tiene trust_env=False para ignorar HTTP_PROXY/HTTPS_PROXY del entorno."""
        import os

        import requests as requests_lib

        from application.media import fetch_external_image

        # Capturar la sesión creada en fetch_external_image
        created_sessions = []
        original_session_init = requests_lib.Session.__init__

        def capturing_init(self, *a, **kw):
            original_session_init(self, *a, **kw)
            created_sessions.append(self)

        old_proxy = os.environ.get("HTTPS_PROXY")
        os.environ["HTTPS_PROXY"] = "http://evil-proxy.test:9999"
        resp = _make_response(200, "image/png", self._TINY_PNG)
        try:
            with patch("requests.Session.__init__", capturing_init):
                with patch(
                    "application.media._resolve_and_validate_host",
                    return_value=([(4, "1.2.3.4")], None),
                ):
                    with patch("requests.Session.get", return_value=resp):
                        fetch_external_image("https://cdn.example.com/logo.png")

            # Verificar que la sesión tiene trust_env=False
            if created_sessions:
                self.assertFalse(
                    created_sessions[-1].trust_env,
                    "Session should have trust_env=False to ignore proxy env vars",
                )
        finally:
            if old_proxy is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = old_proxy

    def test_proxies_empty_dict_passed_to_get(self):
        """fetch_external_image pasa proxies={} a session.get() para nulificar select_proxy()."""
        from application.media import fetch_external_image

        resp = _make_response(200, "image/png", self._TINY_PNG)
        with patch(
            "application.media._resolve_and_validate_host", return_value=([(4, "1.2.3.4")], None)
        ):
            with patch("requests.Session.get", return_value=resp) as mock_get:
                fetch_external_image("https://cdn.example.com/logo.png")

        call_kwargs = mock_get.call_args[1]
        self.assertIn("proxies", call_kwargs)
        self.assertEqual(call_kwargs["proxies"], {})

    def test_adapter_ignores_proxies_kwarg_in_get_connection_with_tls_context(self):
        """_SSRFSafeAdapter.get_connection_with_tls_context() devuelve pool pinneado
        incluso cuando recibe un dict de proxies no vacío."""

        from application.media import _SSRFSafeAdapter

        adapter = _SSRFSafeAdapter(hostname="cdn.example.com", pinned_ip="1.2.3.4")

        # Simular una prepared request
        import requests as req_lib

        prep = req_lib.Request("GET", "https://cdn.example.com/img.png").prepare()

        # Pasar un proxy explícito — el adaptador debe ignorarlo
        proxy_dict = {"https": "http://proxy.evil.com:3128"}
        pool = adapter.get_connection_with_tls_context(prep, verify=True, proxies=proxy_dict)
        # El pool debe ser un _PinnedHTTPSConnectionPool, no un ProxyManager
        from application.media import _PinnedHTTPSConnectionPool

        self.assertIsInstance(pool, _PinnedHTTPSConnectionPool)
        # El host del pool es la IP pinneada
        self.assertEqual(pool.host, "1.2.3.4")

    def test_request_url_returns_relative_path(self):
        """_SSRFSafeAdapter.request_url() siempre devuelve el path relativo,
        incluso cuando hay un proxy configurado."""
        import requests as req_lib

        from application.media import _SSRFSafeAdapter

        adapter = _SSRFSafeAdapter(hostname="cdn.example.com", pinned_ip="1.2.3.4")
        prep = req_lib.Request("GET", "https://cdn.example.com/path/to/image.png?v=1").prepare()

        # Con proxy HTTP definido
        url = adapter.request_url(prep, proxies={"https": "http://proxy.corp:3128"})
        self.assertEqual(url, "/path/to/image.png?v=1")

        # Sin proxy
        url = adapter.request_url(prep, proxies={})
        self.assertEqual(url, "/path/to/image.png?v=1")
