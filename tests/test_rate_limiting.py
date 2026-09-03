import unittest

from werkzeug.middleware.proxy_fix import ProxyFix

import app as application


class RateLimitingTests(unittest.TestCase):
    def test_forwarded_headers_are_ignored_without_proxyfix(self):
        with application.app.test_request_context(
            "/", environ_base={"REMOTE_ADDR": "10.0.0.2"},
            headers={"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.8"},
        ):
            self.assertEqual(application.get_client_ip(), "10.0.0.2")

    def test_proxyfix_uses_client_ip_when_one_proxy_is_trusted(self):
        wrapped = ProxyFix(application.app.wsgi_app, x_for=1)
        environ = {
            "REQUEST_METHOD": "GET", "PATH_INFO": "/", "SERVER_NAME": "localhost",
            "SERVER_PORT": "80", "wsgi.url_scheme": "http", "wsgi.version": (1, 0),
            "wsgi.input": __import__("io").BytesIO(), "wsgi.errors": __import__("sys").stderr,
            "wsgi.multithread": False, "wsgi.multiprocess": False, "wsgi.run_once": False,
            "REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": "203.0.113.9",
        }
        captured = []
        def start_response(status, headers, exc_info=None):
            captured.append(status)
        list(wrapped(environ, start_response))
        self.assertEqual(environ["REMOTE_ADDR"], "203.0.113.9")

    def test_tenant_and_endpoint_buckets_are_separate(self):
        self.assertNotEqual(
            application._rate_limit_key("api:turnos", "1.2.3.4", 1),
            application._rate_limit_key("api:turnos", "1.2.3.4", 2),
        )
        self.assertNotEqual(
            application._rate_limit_key("api:turnos", "1.2.3.4", 1, 10),
            application._rate_limit_key("api:turnos", "1.2.3.4", 1, 11),
        )


if __name__ == "__main__":
    unittest.main()
