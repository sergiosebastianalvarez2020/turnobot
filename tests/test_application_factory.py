"""Tests del application factory (Bloque 6C).

Cubren los contratos arquitectónicos introducidos por create_app():
    C1 - instancia funcional, reutilizable y bien conectada a root_path/templates/static
    C2 - registro central de rutas: 113 rules / 81 endpoints + endpoints canónicos
    C3 - hooks (before_request / after_request), context processors y CSRF
    C4 - security headers inyectados por add_security_headers
    C5 - configuración por defecto/desarrollo
    C6 - guardas de seguridad en modo producción

NOTA (restricción conocida): create_app() debe invocarse SOLO después de que la
fachada `app` esté importada (routes/auth_public.py tiene 'from app import ...'
a nivel de módulo). Por eso se importa `app as application` antes de
`from application import create_app`.
"""

import datetime
import os
import tempfile
import unittest
from pathlib import Path

import flask
from werkzeug.middleware.proxy_fix import ProxyFix

import app as application
import database.database as database
from application import create_app

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_ENV_KEYS = (
    "SECRET_KEY",
    "FLASK_ENV",
    "ADMIN_PASSWORD",
    "ADMIN_PASSWORD_HASH",
    "COOKIE_SECURE",
    "TRUSTED_PROXY_COUNT",
    "SESSION_LIFETIME_SECONDS",
)


class _FactoryIsolationMixin:
    """Aisla la DB (temp) y el entorno del proceso para instancias del factory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self._tmp.name) / "factory.db"
        self._orig_env = {key: os.environ.get(key) for key in _ENV_KEYS}

    def tearDown(self):
        database.DATABASE_PATH = self._orig_db
        self._tmp.cleanup()
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class FactoryContractTests(unittest.TestCase, _FactoryIsolationMixin):
    # C1 — Factory funcional
    def test_create_app_returns_functional_flask_app(self):
        new_app = create_app()
        other = create_app()

        # Instancia Flask funcional, distinta en cada llamada
        self.assertIsInstance(new_app, flask.Flask)
        self.assertIsNot(new_app, other)
        self.assertIsNot(new_app, application.app)

        # root_path apunta a la raíz del proyecto
        self.assertEqual(os.path.normcase(new_app.root_path), os.path.normcase(str(_PROJECT_ROOT)))

        # templates/static configurados y presentes en el repo
        self.assertEqual(new_app.template_folder, "templates")
        self.assertEqual(new_app.static_url_path, "/static")
        self.assertIsNotNone(new_app.static_folder)
        self.assertTrue((_PROJECT_ROOT / "templates").is_dir())
        self.assertTrue((_PROJECT_ROOT / "static").is_dir())

        # La instancia es usable mediante test_client()
        response = new_app.test_client().get("/login")
        self.assertEqual(response.status_code, 200)

    # C2 — URL map
    def test_create_app_registers_expected_url_map(self):
        new_app = create_app()
        rules = list(new_app.url_map.iter_rules())
        endpoints = {rule.endpoint for rule in rules}

        self.assertEqual(len(rules), 113)
        self.assertEqual(len(endpoints), 81)

        for endpoint in (
            "health",
            "index",
            "business_index",
            "business_reservar_wizard",
            "business_reservar_wizard_fecha",
            "business_reservar_wizard_datos",
            "business_reservar_wizard_confirmar",
            "login",
            "registro",
            "admin",
            "admin_slug",
            "superadmin_panel",
            "api_reservar",
            "public_manage_turno",
            "chat",
        ):
            self.assertIn(endpoint, endpoints)

    # C3 — Hooks + CSRF
    def test_create_app_registers_hooks_and_csrf(self):
        new_app = create_app()

        before_request = [
            getattr(func, "__name__", None) for func in new_app.before_request_funcs.get(None, [])
        ]
        after_request = [
            getattr(func, "__name__", None) for func in new_app.after_request_funcs.get(None, [])
        ]
        context_processors = [
            getattr(func, "__name__", None)
            for func in new_app.template_context_processors.get(None, [])
        ]

        self.assertEqual(before_request, ["load_current_business", "_reject_oversized_requests"])
        self.assertEqual(after_request, ["add_security_headers"])
        self.assertIn("inject_admin_prefix", context_processors)
        self.assertIn("inject_business_settings", context_processors)
        self.assertTrue(callable(new_app.jinja_env.globals.get("csrf_token")))

    # C4 — Security headers
    def test_responses_include_security_headers(self):
        new_app = create_app()
        response = new_app.test_client().get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(response.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertEqual(
            response.headers.get("Permissions-Policy"), "camera=(), microphone=(), geolocation=()"
        )


class FactoryConfigTests(unittest.TestCase, _FactoryIsolationMixin):
    # C5 — Configuración de desarrollo/default
    def test_dev_default_config(self):
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["COOKIE_SECURE"] = "1"

        new_app = create_app()

        self.assertTrue(new_app.secret_key)
        self.assertTrue(new_app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(new_app.config["SESSION_COOKIE_SAMESITE"], "Strict")
        self.assertTrue(new_app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(new_app.config["MAX_CONTENT_LENGTH"], 512 * 1024)
        self.assertEqual(
            new_app.config["PERMANENT_SESSION_LIFETIME"], datetime.timedelta(seconds=86400)
        )
        self.assertEqual(new_app.TRUSTED_PROXY_COUNT, 0)
        self.assertNotIsInstance(new_app.wsgi_app, ProxyFix)

    # C6 — Guardas de seguridad en producción
    def test_production_requires_security_config(self):
        def apply_env(replace):
            for key in _ENV_KEYS:
                os.environ.pop(key, None)
            for key, value in replace.items():
                if value is not None:
                    os.environ[key] = value

        cases = (
            ({"FLASK_ENV": "production"}, "sin SECRET_KEY"),
            (
                {"FLASK_ENV": "production", "SECRET_KEY": "x-secret", "ADMIN_PASSWORD": "x"},
                "ADMIN_PASSWORD presente en producción",
            ),
            (
                {"FLASK_ENV": "production", "SECRET_KEY": "x-secret", "COOKIE_SECURE": "1"},
                "sin ADMIN_PASSWORD_HASH",
            ),
            (
                {
                    "FLASK_ENV": "production",
                    "SECRET_KEY": "x-secret",
                    "ADMIN_PASSWORD_HASH": "hash",
                    "COOKIE_SECURE": "0",
                },
                "COOKIE_SECURE != 1",
            ),
        )

        for replace, name in cases:
            with self.subTest(name):
                apply_env(replace)
                with self.assertRaises(RuntimeError):
                    create_app()


if __name__ == "__main__":
    unittest.main()
