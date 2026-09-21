"""Test standalone de create_app() — Bloque 7A.

Demuestra que `application.create_app` es standalone-safe: se importa y se
ejecuta en un proceso Python LIMPIO, donde el módulo de fachada `app` nunca fue
importado. Esto es lo que garantiza que ya no exista dependencia circular
`application` -> `app` (histórico: routes/auth_public.py hacía
`from app import ...` en el top-level).

El test NO usa `import app` para el escenario standalone: lanza un subproceso
con `sys.executable -c` y verifica, en ese proceso aislado:

    1. `application` puede importarse.
    2. `app` NO está en sys.modules en ningún momento (ni antes ni después).
    3. `create_app()` se ejecuta sin errores.
    4. Se obtiene una instancia Flask válida.
    5. El URL map tiene 113 rules / 81 endpoints.
    6. Los endpoints canónicos existen y sus URLs son las esperadas.
    7. Hooks, context processors, CSRF, error handlers y security headers
       siguen enganchados.

Es determinista en Windows y Linux: no usa symlinks, threads ni red. El
baseline de comparación se calcula desde la fachada en el proceso de pytest y
se inyecta al subproceso, de modo que el test falla si el mapa standalone
diverge del mapa de la fachada.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PAYLOAD_MARKER = "__STANDALONE_PAYLOAD__:"

# Entorno mínimo y determinista para el subproceso: sin SECRET_KEY (se genera
# una efímera), sin FLASK_ENV=production (activaría las guardas de config) y
# sin LOG_FORMAT=json.
_STRIPPED_ENV_KEYS = (
    "SECRET_KEY",
    "FLASK_ENV",
    "ADMIN_PASSWORD",
    "ADMIN_PASSWORD_HASH",
    "COOKIE_SECURE",
    "TRUSTED_PROXY_COUNT",
    "SESSION_LIFETIME_SECONDS",
    "LOG_FORMAT",
    "LOG_DIR",
)

_STANDALONE_PROBE = r'''
import json
import sys

from flask import url_for as _flask_url_for

sys.path.insert(0, PROJECT_ROOT)

payload = {"app_imported_before": "app" in sys.modules}

# 1 + 3: importar la factory y ejecutarla sin haber importado `app`
from application import create_app

payload["app_imported_after_import"] = "app" in sys.modules

flask_app = create_app()

# 2: `app` no debe haber sido importado como efecto colateral
payload["app_imported_after_create_app"] = "app" in sys.modules

# 4: instancia Flask válida
payload["is_flask_app"] = flask_app.__class__.__name__ == "Flask"
payload["root_path_is_project_root"] = (
    flask_app.root_path.replace("\\", "/").rstrip("/")
    == PROJECT_ROOT.replace("\\", "/").rstrip("/")
)

# 5: URL map contractual
rules = list(flask_app.url_map.iter_rules())
endpoints = sorted({rule.endpoint for rule in rules})
payload["rules"] = len(rules)
payload["endpoints"] = len(endpoints)
payload["has_rule_fingerprint"] = (
    sorted(rule.rule for rule in rules) == EXPECTED_RULES
)
payload["endpoints_list"] = endpoints

# 6: endpoints canónicos y sus URLs reales
canonical = {}
for endpoint, args in (
    ("health", {}),
    ("login", {}),
    ("index", {}),
    ("admin", {}),
    ("admin_slug", {"slug": "mi-negocio"}),
):
    if endpoint in endpoints:
        with flask_app.test_request_context():
            canonical[endpoint] = _flask_url_for(endpoint, **args)
    else:
        canonical[endpoint] = None
payload["canonical_urls"] = canonical

# 7: hooks, context processors, CSRF y security headers
payload["before_request"] = [
    getattr(func, "__name__", None)
    for func in flask_app.before_request_funcs.get(None, [])
]
payload["after_request"] = [
    getattr(func, "__name__", None)
    for func in flask_app.after_request_funcs.get(None, [])
]
payload["context_processors"] = [
    getattr(func, "__name__", None)
    for func in flask_app.template_context_processors.get(None, [])
]
payload["csrf_token_callable"] = callable(
    flask_app.jinja_env.globals.get("csrf_token")
)
payload["error_handlers"] = sorted(
    code for code in flask_app.error_handler_spec.get(None, {}) if code is not None
)
payload["exception_handler_registered"] = Exception in (
    flask_app.error_handler_spec.get(None, {}).get(None, {})
)

response = flask_app.test_client().get("/health")
payload["health_status"] = response.status_code
payload["security_headers"] = {
    header: response.headers.get(header)
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    )
}

print(MARKER + json.dumps(payload))
'''


class StandaloneCreateAppTests(unittest.TestCase):
    """create_app() funciona en un proceso limpio que nunca importó `app`."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()

        # El baseline (reglas + nombres de hooks) se calcula desde la instancia
        # singleton del proceso de pytest y se inyecta al subproceso como
        # expectativa: el test falla si el mapa standalone diverge del real.
        import app as application

        flask_app = application.app
        cls._expected_rules = sorted(rule.rule for rule in flask_app.url_map.iter_rules())
        cls._expectations = {
            "rules": len(cls._expected_rules),
            "endpoints": len({r.endpoint for r in flask_app.url_map.iter_rules()}),
            "before_request": [
                getattr(func, "__name__", None)
                for func in flask_app.before_request_funcs.get(None, [])
            ],
            "after_request": [
                getattr(func, "__name__", None)
                for func in flask_app.after_request_funcs.get(None, [])
            ],
        }

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run_probe(self):
        """Ejecuta el probe standalone en un subproceso Python limpio."""
        env = os.environ.copy()
        for key in _STRIPPED_ENV_KEYS:
            env.pop(key, None)
        env["PYTHONPATH"] = str(_PROJECT_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["LOG_DIR"] = self._tmp.name

        script = (
            "PROJECT_ROOT = " + repr(str(_PROJECT_ROOT)) + "\n"
            + "EXPECTED_RULES = " + repr(self._expected_rules) + "\n"
            + "MARKER = " + repr(_PAYLOAD_MARKER) + "\n"
            + _STANDALONE_PROBE
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(_PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "El subproceso standalone falló: no se pudo importar "
                "application/ejecutar create_app() sin `app` importado.\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            ),
        )

        payload_line = next(
            (
                line
                for line in reversed(result.stdout.splitlines())
                if line.startswith(_PAYLOAD_MARKER)
            ),
            None,
        )
        self.assertIsNotNone(
            payload_line,
            msg=f"No se encontró el payload standalone.\nSTDOUT:\n{result.stdout}",
        )
        return json.loads(payload_line[len(_PAYLOAD_MARKER):])

    # --- Assertions -------------------------------------------------------
    def test_create_app_runs_standalone_without_app_facade(self):
        payload = self._run_probe()

        # El módulo de fachada nunca fue importado en el proceso limpio
        self.assertFalse(payload["app_imported_before"])
        self.assertFalse(payload["app_imported_after_import"])
        self.assertFalse(payload["app_imported_after_create_app"])

        # Instancia Flask válida, con root_path del proyecto
        self.assertTrue(payload["is_flask_app"])
        self.assertTrue(payload["root_path_is_project_root"])
        self.assertEqual(payload["health_status"], 200)

    def test_standalone_url_map_matches_contract(self):
        payload = self._run_probe()

        # Identidad con el mapa de la fachada y con el contrato histórico
        self.assertEqual(payload["rules"], self._expectations["rules"])
        self.assertEqual(payload["endpoints"], self._expectations["endpoints"])
        self.assertEqual(payload["rules"], 113)
        self.assertEqual(payload["endpoints"], 81)
        # Fingerprint completo del conjunto de rutas
        self.assertTrue(payload["has_rule_fingerprint"])

    def test_standalone_registers_canonical_endpoints(self):
        payload = self._run_probe()
        canonical = payload["canonical_urls"]

        self.assertEqual(canonical["health"], "/health")
        self.assertEqual(canonical["login"], "/login")
        self.assertEqual(canonical["index"], "/")
        self.assertEqual(canonical["admin"], "/admin")
        self.assertEqual(canonical["admin_slug"], "/b/mi-negocio/admin")

    def test_standalone_preserves_hooks_csrf_and_security_headers(self):
        payload = self._run_probe()

        self.assertEqual(
            payload["before_request"], self._expectations["before_request"]
        )
        self.assertEqual(
            payload["after_request"], self._expectations["after_request"]
        )
        self.assertEqual(
            payload["before_request"],
            ["load_current_business", "_reject_oversized_requests"],
        )
        self.assertEqual(payload["after_request"], ["add_security_headers"])
        self.assertIn("inject_admin_prefix", payload["context_processors"])
        self.assertIn("inject_business_settings", payload["context_processors"])
        self.assertTrue(payload["csrf_token_callable"])

        # Error handlers (400/403/404/405/413/429/500 + Exception)
        self.assertEqual(payload["error_handlers"], [400, 403, 404, 405, 413, 429, 500])
        self.assertTrue(payload["exception_handler_registered"])

        # Smoke: /health responde y aplica security headers
        headers = payload["security_headers"]
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(
            headers["Referrer-Policy"], "strict-origin-when-cross-origin"
        )
        self.assertEqual(
            headers["Permissions-Policy"],
            "camera=(), microphone=(), geolocation=()",
        )


if __name__ == "__main__":
    unittest.main()

