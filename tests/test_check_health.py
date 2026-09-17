"""Tests del healthcheck HTTP de TurnoBot (scripts/check_health.py).

Cubren:
    * HTTP 200 -> exit 0;
    * HTTP 503 -> exit 1;
    * timeout sin respuesta -> exit 1;
    * reintentos hasta agotar;
    * éxito después de un fallo inicial;
    * uso de --url / --attempts / --delay / --timeout;
    * ausencia de secretos en stdout/stderr.

Usa únicamente stdlib (http.server local en loopback), sin red externa
y sin dependencias nuevas.
"""

import contextlib
import http.server
import io
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_health as ch


class SequentialHandler(http.server.BaseHTTPRequestHandler):
    """Responde las respuestas en cola, en orden; 404 cuando se agota."""

    responses = []

    def do_GET(self):
        if not self.responses:
            status, body = 404, b"not found"
        else:
            status, body = self.responses.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HangServer(http.server.ThreadingHTTPServer):
    """Servidor que acepta conexiones y nunca responde (para timeout)."""


class HangHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(30)

    def log_message(self, *args):
        pass


def start_sequential(responses):
    SequentialHandler.responses = responses
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SequentialHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/health"


def start_hanging():
    server = HangServer(("127.0.0.1", 0), HangHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/health"


def run_main(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ch.main(args)
    return code, out.getvalue(), err.getvalue()


class TestCheckHealthScript(unittest.TestCase):
    def setUp(self):
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def _start(self, responses):
        server, url = start_sequential(responses)
        self._servers.append(server)
        return url

    def test_http_200_exit_0(self):
        url = self._start([(200, b'{"status":"ok","database":"ok"}')])
        code, out, err = run_main(["--url", url, "--attempts", "1"])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        self.assertEqual(err, "")

    def test_http_503_exit_nonzero(self):
        url = self._start([(503, b'{"status":"error","database":"error"}')] * 3)
        code, out, err = run_main(["--url", url, "--attempts", "3", "--delay", "0"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", err)
        self.assertNotIn("OK", out)

    def test_timeout_exit_nonzero(self):
        server, url = start_hanging()
        self._servers.append(server)
        code, _, err = run_main(["--url", url, "--attempts", "1", "--timeout", "1"])
        self.assertEqual(code, 1)
        self.assertIn("sin respuesta HTTP", err)

    def test_retries_until_exhausted(self):
        url = self._start([(503, b"x")] * 5)
        remaining_before = len(SequentialHandler.responses)
        code, _, err = run_main(["--url", url, "--attempts", "3", "--delay", "0"])
        self.assertEqual(code, 1)
        consumed = remaining_before - len(SequentialHandler.responses)
        self.assertEqual(consumed, 3)
        self.assertIn("3", err)

    def test_success_after_initial_failure(self):
        url = self._start([(503, b"x"), (200, b'{"status":"ok"}')])
        code, out, _ = run_main(["--url", url, "--attempts", "3", "--delay", "0"])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        self.assertEqual(SequentialHandler.responses, [], "no debe usar el tercer intento")

    def test_args_are_used(self):
        url = self._start([(200, b'{"status":"ok"}')])
        code, _, _ = run_main(["--url", url, "--attempts", "1"])
        self.assertEqual(code, 0)

    def test_invalid_attempts_returns_2(self):
        code, _, err = run_main(["--attempts", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--attempts", err)

    def test_no_secrets_in_output(self):
        os.environ["SMTP_PASSWORD"] = "contraseña-top-secret-987"
        try:
            url = self._start([(503, b"x")])
            _, out, err = run_main(["--url", url, "--attempts", "2", "--delay", "0"])
            combined = out + err
            self.assertNotIn("contraseña-top-secret-987", combined)
            self.assertNotIn("SMTP", combined)
            self.assertNotIn("PASSWORD", combined)
        finally:
            del os.environ["SMTP_PASSWORD"]


class TestCheckHealthFunctions(unittest.TestCase):
    def test_ok_returns_ok(self):
        server, url = start_sequential([(200, b'{"status":"ok"}')])
        try:
            ok, intentos = ch.check_health(url=url, attempts=1,
                                           timeout=5, delay=0)
            self.assertTrue(ok)
            self.assertEqual(intentos, [200])
        finally:
            server.shutdown()
            server.server_close()

    def test_failure_returns_failure(self):
        server, url = start_sequential([(503, b"x"), (503, b"x")])
        try:
            ok, intentos = ch.check_health(url=url, attempts=2,
                                           timeout=5, delay=0)
            self.assertFalse(ok)
            self.assertEqual(intentos, [503, 503])
        finally:
            server.shutdown()
            server.server_close()

    def test_sanitize_enmascara_secretos(self):
        self.assertEqual(ch.sanitize("SMTP_PASSWORD=abc"), "***_***=abc")
        self.assertNotIn("PASSWORD", ch.sanitize("error PASSWORD x TOKEN"))

    def test_probe_no_devuelve_cuerpo(self):
        server, url = start_sequential([(200, b'{"valor": "muy-secreto"}')])
        try:
            code, motivo = ch.probe(url, timeout=5)
            self.assertEqual(code, 200)
            self.assertEqual(motivo, "")
        finally:
            server.shutdown()
            server.server_close()

    def test_default_url_es_loopback(self):
        self.assertEqual(ch.DEFAULT_URL, "http://127.0.0.1:5000/health")


if __name__ == "__main__":
    unittest.main()