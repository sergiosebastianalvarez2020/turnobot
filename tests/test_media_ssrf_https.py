"""Tests de integración HTTPS para _SSRFSafeAdapter y _PinnedHTTPSConnectionPool.

Estos tests levantan un servidor HTTPS local en un puerto dinámico usando
certificados X.509 generados en memoria con `cryptography`. No requieren
acceso a Internet ni certificados instalados en el sistema.

Comprueban:
1. Que la conexión TCP se establece contra la IP pinneada (127.0.0.1).
2. Que el servidor recibe el SNI del hostname original, no la IP.
3. Que el servidor recibe el header Host con el hostname original.
4. Que un certificado válido para el hostname es aceptado.
5. Que un certificado emitido para otro hostname es rechazado (SSLError).
6. Que un certificado firmado por una CA no confiable es rechazado (SSLError).
7. Que las variables de entorno HTTPS_PROXY/HTTP_PROXY no afectan la conexión
   cuando trust_env=False.
8. Que el socket de la conexión está conectado a la IP pinneada (no a otra).
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import os
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.no_pg


# ---------------------------------------------------------------------------
# PKI de prueba: CA raíz + certificado de servidor firmado por esa CA.
# ---------------------------------------------------------------------------


def _build_test_pki(hostname: str, server_ip: str = "127.0.0.1") -> tuple[str, str, str, str]:
    """Genera una PKI de prueba completa en un directorio temporal.

    Returns (ca_pem_path, srv_cert_path, srv_key_path, tmpdir).
    Los archivos se eliminan cuando el caller borra el directorio.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    # --- CA ---
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TurnoBot Test CA")])
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # --- Certificado de servidor firmado por CA ---
    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(hostname),
                    x509.IPAddress(ipaddress.IPv4Address(server_ip)),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    tmpdir = tempfile.mkdtemp(prefix="turnobot_tls_test_")
    ca_pem = Path(tmpdir) / "ca.pem"
    srv_crt = Path(tmpdir) / "srv.crt"
    srv_key_pem = Path(tmpdir) / "srv.key"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    srv_crt.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    srv_key_pem.write_bytes(
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(ca_pem), str(srv_crt), str(srv_key_pem), tmpdir


# ---------------------------------------------------------------------------
# Servidor HTTPS local mínimo con captura de SNI, Host y peername.
# ---------------------------------------------------------------------------


class _LocalHTTPSServer:
    """Servidor HTTPS local que registra SNI, Host y conexión del cliente."""

    def __init__(self, srv_crt: str, srv_key: str, ip: str = "127.0.0.1"):
        self.observed: dict = {}
        self._sni_received: list[str] = []

        def sni_callback(sslobj, server_name, ctx):
            if server_name:
                self._sni_received.append(server_name)
            return None  # continuar handshake

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(srv_crt, srv_key)
        ctx.sni_callback = sni_callback

        obs = self.observed
        sni_list = self._sni_received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                obs["path"] = self.path
                obs["host_header"] = self.headers.get("Host", "")
                obs["peername"] = self.connection.getpeername()
                obs["sni"] = sni_list[-1] if sni_list else None
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"fake")

            def log_message(self, *a):
                pass

        raw_server = http.server.HTTPServer((ip, 0), Handler)
        raw_server.socket = ctx.wrap_socket(raw_server.socket, server_side=True)
        self._server = raw_server
        self.port = raw_server.socket.getsockname()[1]
        self.ip = ip

    def handle_one(self) -> threading.Thread:
        """Procesa exactamente una solicitud en un hilo daemon.

        Establece un timeout en el server socket para evitar que handle_request()
        bloquee indefinidamente cuando hay contención de TLS entre tests consecutivos.
        """
        import time
        self._server.timeout = 8  # evitar bloqueo indefinido en SSL handshake
        self._server.socket.settimeout(8)
        ready = threading.Event()

        def _serve():
            ready.set()
            self._server.handle_request()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        ready.wait(timeout=2)  # esperar a que el hilo esté listo
        time.sleep(0.05)       # dar tiempo al SO para que el socket quede en select()
        return t

    def close(self, thread: threading.Thread | None = None):
        if thread:
            thread.join(timeout=3)
        self._server.server_close()


# ---------------------------------------------------------------------------
# Clase base de test con PKI compartida.
# ---------------------------------------------------------------------------

_HOSTNAME = "cdn.test.local"
_PKI: tuple | None = None  # (ca_pem, srv_crt, srv_key, tmpdir)
_WRONG_PKI: tuple | None = None  # cert for a DIFFERENT hostname
_OTHER_CA_PKI: tuple | None = None  # same hostname, different CA


def _get_pki():
    global _PKI
    if _PKI is None:
        _PKI = _build_test_pki(_HOSTNAME)
    return _PKI


def _get_wrong_pki():
    global _WRONG_PKI
    if _WRONG_PKI is None:
        _WRONG_PKI = _build_test_pki("other.host.local")
    return _WRONG_PKI


def _get_other_ca_pki():
    global _OTHER_CA_PKI
    if _OTHER_CA_PKI is None:
        _OTHER_CA_PKI = _build_test_pki(_HOSTNAME)
    return _OTHER_CA_PKI


class TestSSRFAdapterHTTPS(unittest.TestCase):
    """Tests de integración HTTPS para _SSRFSafeAdapter."""

    def _make_session(self, hostname: str = _HOSTNAME, pinned_ip: str = "127.0.0.1"):
        from application.media import _SSRFSafeAdapter

        session = requests.Session()
        session.trust_env = False
        adapter = _SSRFSafeAdapter(hostname=hostname, pinned_ip=pinned_ip)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get(self, session, port, path="/image.png", verify=None):
        ca_pem, _, _, _ = _get_pki()
        return session.get(
            f"https://{_HOSTNAME}:{port}{path}",
            verify=verify or ca_pem,
            allow_redirects=False,
            timeout=5,
            proxies={},
        )

    # -------------------------------------------------------------------
    # Test 1: conexión exitosa con cert válido.
    # -------------------------------------------------------------------
    def test_connects_to_pinned_ip(self):
        """El adaptador conecta TCP a la IP pinneada (127.0.0.1)."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session()
            resp = self._get(session, srv.port)
            self.assertEqual(resp.status_code, 200)
            # El servidor vio al cliente conectarse desde 127.0.0.1
            self.assertEqual(srv.observed.get("peername", ("",))[0], "127.0.0.1")
        finally:
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 2: SNI contiene el hostname original, no la IP.
    # -------------------------------------------------------------------
    def test_sni_contains_original_hostname(self):
        """El TLS ClientHello envía SNI con el hostname original, no con la IP."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session()
            resp = self._get(session, srv.port)
            self.assertEqual(resp.status_code, 200)
            sni = srv.observed.get("sni")
            self.assertEqual(sni, _HOSTNAME, f"SNI expected {_HOSTNAME!r} but got {sni!r}")
        finally:
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 3: header Host contiene el hostname original, no la IP.
    # -------------------------------------------------------------------
    def test_host_header_contains_original_hostname(self):
        """El header HTTP Host contiene el hostname original, no la IP numérica."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session()
            resp = self._get(session, srv.port)
            self.assertEqual(resp.status_code, 200)
            host_header = srv.observed.get("host_header", "")
            self.assertTrue(
                host_header.startswith(_HOSTNAME),
                f"Host header expected to start with {_HOSTNAME!r} but got {host_header!r}",
            )
            # La IP no debe aparecer en el Host header
            self.assertNotIn("127.0.0.1", host_header)
        finally:
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 4: certificado válido para el hostname es aceptado.
    # -------------------------------------------------------------------
    def test_valid_cert_accepted(self):
        """Un certificado válido firmado por la CA de prueba es aceptado."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session()
            resp = self._get(session, srv.port, verify=ca_pem)
            self.assertEqual(resp.status_code, 200)
        finally:
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 5: certificado para otro hostname es rechazado.
    # -------------------------------------------------------------------
    def test_wrong_hostname_cert_rejected(self):
        """Un certificado emitido para 'other.host.local' es rechazado con SSLError."""
        ca_pem, _, _, _ = _get_pki()
        _, wrong_srv_crt, wrong_srv_key, _ = _get_wrong_pki()
        srv = _LocalHTTPSServer(wrong_srv_crt, wrong_srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session()
            with self.assertRaises(requests.exceptions.SSLError):
                self._get(session, srv.port, verify=ca_pem)
        finally:
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 6: certificado firmado por CA no confiable es rechazado.
    # -------------------------------------------------------------------
    def test_untrusted_ca_cert_rejected(self):
        """Un certificado firmado por una CA diferente (no confiable) es rechazado."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        _, _, _, other_tmpdir = _get_other_ca_pki()
        other_ca_pem = str(Path(other_tmpdir) / "ca.pem")
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session()
            with self.assertRaises(requests.exceptions.SSLError):
                self._get(session, srv.port, verify=other_ca_pem)
        finally:
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 7: variables de entorno de proxy son ignoradas.
    # -------------------------------------------------------------------
    def test_proxy_env_vars_ignored_with_trust_env_false(self):
        """HTTPS_PROXY y http_proxy del entorno no afectan la conexión cuando trust_env=False."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        old_https = os.environ.get("HTTPS_PROXY")
        old_http = os.environ.get("HTTP_PROXY")
        try:
            # Configurar proxies del entorno que apuntan a hosts inexistentes.
            os.environ["HTTPS_PROXY"] = "http://nonexistent-proxy.invalid:8080"
            os.environ["HTTP_PROXY"] = "http://nonexistent-proxy.invalid:8080"
            session = self._make_session()
            # La conexión debe ir directamente a la IP pinneada, ignorando los proxies.
            resp = self._get(session, srv.port, verify=ca_pem)
            self.assertEqual(resp.status_code, 200)
            # El Host header sigue siendo correcto.
            self.assertTrue(srv.observed.get("host_header", "").startswith(_HOSTNAME))
        finally:
            if old_https is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = old_https
            if old_http is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old_http
            srv.close(t)

    # -------------------------------------------------------------------
    # Test 8: el socket está conectado exactamente a la IP pinneada.
    # -------------------------------------------------------------------
    def test_socket_connected_to_pinned_ip(self):
        """El socket de la conexión está conectado a la IP pinneada, no a otra."""
        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = self._make_session(pinned_ip="127.0.0.1")
            resp = self._get(session, srv.port, verify=ca_pem)
            self.assertEqual(resp.status_code, 200)
            # El servidor vio al cliente llegar desde 127.0.0.1 (la IP pinneada).
            peername = srv.observed.get("peername", ("unknown",))
            self.assertEqual(
                peername[0],
                "127.0.0.1",
                f"Expected connection from 127.0.0.1 but got {peername[0]}",
            )
        finally:
            srv.close(t)


class TestSSRFAdapterHTTP(unittest.TestCase):
    """Tests de integración HTTP (sin TLS) para _SSRFSafeAdapter."""

    def _make_session(self, hostname: str, pinned_ip: str = "127.0.0.1"):
        from application.media import _SSRFSafeAdapter

        session = requests.Session()
        session.trust_env = False
        adapter = _SSRFSafeAdapter(hostname=hostname, pinned_ip=pinned_ip)
        session.mount("http://", adapter)
        return session

    def test_http_host_header_and_pinned_ip(self):
        """HTTP: el servidor recibe Host correcto y la conexión viene de la IP pinneada."""
        HOSTNAME_HTTP = "img.test.local"
        observed: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                observed["host_header"] = self.headers.get("Host", "")
                observed["peername"] = self.connection.getpeername()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"fake")

            def log_message(self, *a):
                pass

        raw_server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = raw_server.socket.getsockname()[1]
        t = threading.Thread(target=raw_server.handle_request, daemon=True)
        t.start()
        try:
            session = self._make_session(hostname=HOSTNAME_HTTP, pinned_ip="127.0.0.1")
            resp = session.get(
                f"http://{HOSTNAME_HTTP}:{port}/image.png",
                allow_redirects=False,
                timeout=5,
                proxies={},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(
                observed.get("host_header", "").startswith(HOSTNAME_HTTP),
                f"Expected Host: {HOSTNAME_HTTP} but got {observed.get('host_header')}",
            )
            self.assertEqual(observed.get("peername", ("",))[0], "127.0.0.1")
        finally:
            t.join(timeout=2)
            raw_server.server_close()

    def test_http_proxy_env_ignored(self):
        """HTTP: HTTP_PROXY del entorno no afecta la conexión cuando proxies={}."""
        HOSTNAME_HTTP = "img2.test.local"
        observed: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                observed["host_header"] = self.headers.get("Host", "")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"fake")

            def log_message(self, *a):
                pass

        raw_server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = raw_server.socket.getsockname()[1]
        t = threading.Thread(target=raw_server.handle_request, daemon=True)
        t.start()
        old = os.environ.get("HTTP_PROXY")
        try:
            os.environ["HTTP_PROXY"] = "http://nonexistent-proxy.invalid:8080"
            session = self._make_session(hostname=HOSTNAME_HTTP, pinned_ip="127.0.0.1")
            resp = session.get(
                f"http://{HOSTNAME_HTTP}:{port}/image.png",
                allow_redirects=False,
                timeout=5,
                proxies={},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(observed.get("host_header", "").startswith(HOSTNAME_HTTP))
        finally:
            if old is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old
            t.join(timeout=2)
            raw_server.server_close()


class TestSSRFAdapterNoSecondDNS(unittest.TestCase):
    """Verifica que no se realiza una segunda resolución DNS durante la conexión."""

    def test_no_dns_resolution_during_connection(self):
        """Una vez pinneada la IP, no se llama a socket.getaddrinfo durante la conexión TCP."""
        from unittest.mock import patch

        from application.media import _SSRFSafeAdapter

        ca_pem, srv_crt, srv_key, _ = _get_pki()
        srv = _LocalHTTPSServer(srv_crt, srv_key)
        t = srv.handle_one()
        try:
            session = requests.Session()
            session.trust_env = False
            adapter = _SSRFSafeAdapter(hostname=_HOSTNAME, pinned_ip="127.0.0.1")
            session.mount("https://", adapter)

            # Registrar cualquier llamada a getaddrinfo DESPUÉS de que el adaptador
            # ya tiene la IP pinneada. Permitir que el call original ejecute pero
            # registrar si ocurrió.
            dns_calls: list = []
            original_getaddrinfo = socket.getaddrinfo

            def tracked_getaddrinfo(*args, **kwargs):
                dns_calls.append(args)
                return original_getaddrinfo(*args, **kwargs)

            with patch("socket.getaddrinfo", side_effect=tracked_getaddrinfo):
                resp = session.get(
                    f"https://{_HOSTNAME}:{srv.port}/image.png",
                    verify=ca_pem,
                    allow_redirects=False,
                    timeout=5,
                    proxies={},
                )

            self.assertEqual(resp.status_code, 200)
            # El hostname original NO debe aparecer en ninguna llamada a getaddrinfo
            # durante la conexión (ya tiene la IP pinneada, no necesita resolver).
            hostname_resolved_during_connect = any(
                _HOSTNAME in str(args) for args in dns_calls
            )
            self.assertFalse(
                hostname_resolved_during_connect,
                f"getaddrinfo was called with the original hostname during connection: {dns_calls}",
            )
        finally:
            srv.close(t)
