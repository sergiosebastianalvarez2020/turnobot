"""Infraestructura compartida para escaneos axe-core en tests visuales (Fase 1).

El motor JavaScript se inyecta desde ``static/vendor/axe.min.js`` (asset local,
sin CDN). Los escaneos funcionan en modo WARN: las violaciones se registran en
JSON y en consola, pero nunca hacen fallar el test ni el job de CI.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AXE_JS_PATH = PROJECT_ROOT / "static" / "vendor" / "axe.min.js"
AXE_RESULTS_DIR = Path(__file__).parent / "results" / "axe"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]

SUPERADMIN_EMAIL = "axe-superadmin@test.local"
SUPERADMIN_PASSWORD = "axe-superadmin-pass-123"


def run_axe_scan(page: Page, *, route: str, viewport: str) -> dict:
    """Inyecta axe desde el archivo local y devuelve un informe JSON en modo WARN."""
    if not AXE_JS_PATH.is_file():
        raise RuntimeError(f"axe-core local no encontrado: {AXE_JS_PATH}")

    # CSP del proyecto: script-src 'self' 'unsafe-inline' -> la inyección local es válida.
    page.add_script_tag(path=str(AXE_JS_PATH))
    axe_version = page.evaluate("() => window.axe && window.axe.version")
    if not axe_version:
        raise RuntimeError("axe-core no se cargó desde el asset local; no se puede escanear")

    raw = page.evaluate(
        """async (options) => await window.axe.run(document, options)""",
        {"runOnly": {"type": "tag", "values": AXE_TAGS}},
    )

    now = datetime.now(UTC)
    report = {
        "axeVersion": axe_version,
        "route": route,
        "viewport": viewport,
        "timestamp": now.isoformat(),
        "violations": raw.get("violations", []),
        "incomplete": raw.get("incomplete", []),
        "passesCount": len(raw.get("passes", [])),
    }

    AXE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_route = route.strip("/").replace("/", "__") or "root"
    stamp = now.strftime("%Y%m%d-%H%M%S")
    report_path = AXE_RESULTS_DIR / f"{safe_route}-{viewport}-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    by_impact: dict[str, int] = {}
    for violation in report["violations"]:
        impact = violation.get("impact") or "minor"
        by_impact[impact] = by_impact.get(impact, 0) + 1
    print(
        f"[axe][WARN] {route} ({viewport}): "
        f"violations={len(report['violations'])} incomplete={len(report['incomplete'])} "
        f"passes={report['passesCount']} byImpact={by_impact}"
    )
    print(f"[axe][WARN] informe: {report_path}")
    return report


def start_isolated_flask_app_with_superadmin():
    """Arranca Flask en SQLite temporal con negocio 1, owner y superadmin de prueba."""
    project_root = PROJECT_ROOT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    app_temp_dir = tempfile.TemporaryDirectory(prefix="turnobot-visual-axe-")
    temp_root = Path(app_temp_dir.name).resolve()
    database_path = (temp_root / "appointments.sqlite3").resolve()
    default_database_path = (project_root / "database" / "appointments.db").resolve()
    if database_path == default_database_path:
        app_temp_dir.cleanup()
        raise RuntimeError("Visual axe test database must not use the normal TurnoBot database")

    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")
        if key in os.environ
    }
    env.update(
        {
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "FLASK_ENV": "testing",
            "SESSION_LIFETIME_SECONDS": "3600",
            "DB_BACKEND": "sqlite",
            "DATABASE_URL": "",
            "SECRET_KEY": "test-secret-key-for-visual-axe-tests",
            "PUBLIC_BASE_URL": "http://127.0.0.1",
            "TRUSTED_PROXY_COUNT": "0",
            "LOG_DIR": str(temp_root / "logs"),
            "TURNOBOT_VISUAL_PROJECT_ROOT": str(project_root),
            "TURNOBOT_VISUAL_DATABASE_PATH": str(database_path),
            "TURNOBOT_VISUAL_SUPERADMIN_EMAIL": SUPERADMIN_EMAIL,
            "TURNOBOT_VISUAL_SUPERADMIN_PASSWORD": SUPERADMIN_PASSWORD,
        }
    )

    ready_file = temp_root / "server-ready"
    env["TURNOBOT_VISUAL_PORT"] = str(port)
    env["TURNOBOT_VISUAL_READY_FILE"] = str(ready_file)

    child_script = """
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["TURNOBOT_VISUAL_PROJECT_ROOT"])

from database import database

database.DATABASE_PATH = Path(os.environ["TURNOBOT_VISUAL_DATABASE_PATH"]).resolve()
expected_database = Path(os.environ["TURNOBOT_VISUAL_DATABASE_PATH"]).resolve()
default_database = (
    Path(os.environ["TURNOBOT_VISUAL_PROJECT_ROOT"]) / "database" / "appointments.db"
).resolve()
if database.DATABASE_PATH != expected_database or database.DATABASE_PATH == default_database:
    raise RuntimeError("Refusing to initialize a non-temporary visual-axe database")
if os.environ.get("DB_BACKEND") != "sqlite" or os.environ.get("DATABASE_URL"):
    raise RuntimeError("Visual-axe subprocess must use SQLite without a database URL")

from application import create_app
from services import platform as platform_service

app = create_app()
app.config["TESTING"] = True
if app.config.get("DB_BACKEND") != "sqlite" or app.config.get("DATABASE_URL"):
    raise RuntimeError("Flask did not retain the isolated SQLite test configuration")

connection = database.get_connection()
try:
    connection.execute(
        "INSERT OR IGNORE INTO businesses(id, name, slug) VALUES(1, 'El Corte', 'el-corte')"
    )
    connection.commit()
finally:
    connection.close()

created = platform_service.create_superadmin(
    os.environ["TURNOBOT_VISUAL_SUPERADMIN_EMAIL"],
    os.environ["TURNOBOT_VISUAL_SUPERADMIN_PASSWORD"],
    "Visual QA Superadmin",
)
if not created["success"]:
    raise RuntimeError(f"Could not create test superadmin: {created['reason']}")

from waitress import create_server

server = create_server(app, host="127.0.0.1", port=int(os.environ["TURNOBOT_VISUAL_PORT"]))
Path(os.environ["TURNOBOT_VISUAL_READY_FILE"]).write_text("ready", encoding="utf-8")
server.run()
"""
    cmd = [sys.executable, "-c", child_script]
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, env=env, cwd=str(project_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(100):
            if ready_file.exists():
                return proc, app_temp_dir, f"http://127.0.0.1:{port}"
            if proc.poll() is not None:
                break
            time.sleep(0.2)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        stdout, stderr = proc.communicate()
        so = stdout.decode("utf-8", errors="replace") if stdout else ""
        se = stderr.decode("utf-8", errors="replace") if stderr else ""
        raise RuntimeError(f"Flask app failed to start:\nSTDOUT: {so}\nSTDERR: {se}")
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        app_temp_dir.cleanup()
        raise


@pytest.fixture(scope="session")
def axe_asset_available() -> Path:
    """Falla temprano si el asset vendorizado falta (instrumentación, no accesibilidad)."""
    if not AXE_JS_PATH.is_file():
        pytest.fail(f"Falta el asset local de axe-core: {AXE_JS_PATH}")
    return AXE_JS_PATH


@pytest.fixture
def axe_scan():
    """Expone el escaneo en modo WARN a todos los tests visuales."""
    return run_axe_scan


@pytest.fixture
def superadmin_credentials():
    """Credenciales fijas y ficticias del superadmin creado en la BD temporal."""
    return SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD


@pytest.fixture
def isolated_superadmin_app():
    """Base URL de una app efímera con superadmin; la BD temporal se limpia al salir."""
    proc, app_temp_dir, base_url = start_isolated_flask_app_with_superadmin()
    try:
        yield base_url
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        finally:
            app_temp_dir.cleanup()
