"""Visual responsive tests for the TurnoBot public landing page using Playwright.

Run with:
    python -m pytest tests/visual/ -v --tb=short -m no_pg

Markers:
    no_pg: test independiente de PostgreSQL (no requiere base de datos).
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

RESOLUTIONS = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "mobile", "width": 390, "height": 844},
]

TEST_BUSINESS_SLUG = "el-corte"
TEST_BUSINESS_NAME = "El Corte"

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def _capture_screenshot(page, name: str) -> Path:
    """Capture a screenshot and return the path."""
    path = RESULTS_DIR / f"{name}.png"
    page.screenshot(path=path, full_page=True)
    return path


def _has_horizontal_overflow(page) -> bool:
    """Detect if document has accidental horizontal overflow."""
    result = page.evaluate(
        """() => {
      const body = document.body;
      const html = document.documentElement;
      const viewportWidth = Math.max(window.innerWidth, html.clientWidth);
      const docWidth = Math.max(body.scrollWidth, html.scrollWidth, body.offsetWidth, html.offsetWidth);
      if (docWidth > viewportWidth + 1) {
        const scrollables = Array.from(document.querySelectorAll('.table-container, .smtp-instructions, [data-scroll-container]'));
        let allowedOverflow = 0;
        for (const sc of scrollables) {
          const rect = sc.getBoundingClientRect();
          if (rect.width > 0 && rect.left >= -2 && rect.right <= viewportWidth + 2) {
            const sw = sc.scrollWidth;
            const cw = sc.clientWidth;
            if (sw > cw) {
              allowedOverflow += (sw - cw);
            }
          }
        }
        if (docWidth - allowedOverflow > viewportWidth + 2) {
          return true;
        }
      }
      const elements = document.querySelectorAll('*');
      for (const el of elements) {
        if (el.closest('.table-container, .smtp-instructions')) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.right > viewportWidth + 2) {
          const style = window.getComputedStyle(el);
          if (style.position === 'fixed' || style.position === 'absolute') {
            continue;
          }
          let parent = el.parentElement;
          let inScrollable = false;
          while (parent) {
            if (parent.classList && (parent.classList.contains('table-container') || parent.classList.contains('smtp-instructions') || parent.getAttribute('data-scroll-container'))) {
              inScrollable = true;
              break;
            }
            parent = parent.parentElement;
          }
          if (inScrollable) continue;
          return true;
        }
      }
      return false;
    }"""
    )
    return bool(result)


def _capture_browser_issues(page) -> dict:
    """Capture browser issues from the first navigation onward."""
    issues = {"console": [], "page_errors": [], "request_failures": [], "http_errors": []}

    def handle_console(msg):
        if msg.type() == "error":
            issues["console"].append({"text": msg.text, "location": msg.location()})

    page.on("console", handle_console)
    page.on("pageerror", lambda error: issues["page_errors"].append(str(error)))
    page.on(
        "requestfailed",
        lambda request: issues["request_failures"].append(
            {"url": request.url, "error": request.failure}
        ),
    )
    page.on(
        "response",
        lambda response: (
            issues["http_errors"].append({"status": response.status, "url": response.url})
            if response.status >= 400
            else None
        ),
    )
    return issues


def _start_flask_app():
    """Start an isolated Flask app with its SQLite database in a temp directory."""
    project_root = Path(__file__).resolve().parents[2]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    app_temp_dir = tempfile.TemporaryDirectory(prefix="turnobot-visual-landing-")
    temp_root = Path(app_temp_dir.name).resolve()
    database_path = (temp_root / "appointments.sqlite3").resolve()
    default_database_path = (project_root / "database" / "appointments.db").resolve()
    if database_path == default_database_path:
        app_temp_dir.cleanup()
        raise RuntimeError("Visual test database must not use the normal TurnoBot database")

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
            "SECRET_KEY": "test-secret-key-for-visual-tests",
            "PUBLIC_BASE_URL": "http://127.0.0.1",
            "TRUSTED_PROXY_COUNT": "0",
            "LOG_DIR": str(temp_root / "logs"),
            "ADMIN_TEST_EMAIL": "owner@test.local",
            "ADMIN_TEST_PASSWORD": "testpass123",
            "TURNOBOT_VISUAL_PROJECT_ROOT": str(project_root),
            "TURNOBOT_VISUAL_DATABASE_PATH": str(database_path),
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
    raise RuntimeError("Refusing to initialize a non-temporary visual-test database")
if os.environ.get("DB_BACKEND") != "sqlite" or os.environ.get("DATABASE_URL"):
    raise RuntimeError("Visual-test subprocess must use SQLite without a database URL")

from database import seed_auth
from werkzeug.security import generate_password_hash
from application import create_app

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

seed_auth.provision_owner_from_bootstrap(
    1,
    os.environ["ADMIN_TEST_EMAIL"],
    generate_password_hash(os.environ["ADMIN_TEST_PASSWORD"]),
)

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


@pytest.mark.no_pg
def test_public_landing_responsive():
    """Test the public landing page responsiveness and visual integrity.

    Runs against all three resolutions: desktop, tablet, mobile.
    This test:
    - Uses create_app() for an isolated Flask instance
    - Does NOT start or modify the production application
    - Does NOT access AWS or production servers
    - Uses only fixed test credentials created in the isolated database
    - Does NOT contain real credentials, tokens, or secrets
    - Does not read credentials from the parent process environment
    - Verifies the marketing landing page at /
    - Verifies the business landing page at /b/el-corte
    - Checks for horizontal overflow
    - Captures console errors
    - Captures screenshots at each resolution
    """
    flask_proc, app_temp_dir, base_url = _start_flask_app()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            pages_to_test = [
                ("/", "marketing-home"),
                (f"/b/{TEST_BUSINESS_SLUG}", "business-landing"),
            ]

            for resolution in RESOLUTIONS:
                name = resolution["name"]
                width = resolution["width"]
                height = resolution["height"]

                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()

                browser_issues = _capture_browser_issues(page)

                for path, page_label in pages_to_test:
                    page.goto(f"{base_url}{path}")
                    page.wait_for_load_state("networkidle", timeout=15000)

                    # Verify page title and business name are present
                    page_title = page.title()
                    assert page_title, f"{page_label} ({name}): empty page title"

                    # Check for business name in the page content
                    body_text = page.locator("body").inner_text()
                    assert TEST_BUSINESS_NAME in body_text or "TurnoBot" in body_text, (
                        f"{page_label} ({name}): business name not found in page content"
                    )

                    # Check for horizontal overflow
                    has_overflow = _has_horizontal_overflow(page)
                    overflow_is_error = page_label == "business-landing"
                    if has_overflow:
                        if overflow_is_error:
                            print(f"  ERROR: {page_label} ({name}): Horizontal overflow detected")
                        else:
                            print(
                                f"  WARNING: {page_label} ({name}): Horizontal overflow detected (marketing page)"
                            )
                    else:
                        print(f"  {page_label} ({name}): no accidental document overflow")
                    if overflow_is_error:
                        assert not has_overflow, (
                            f"{page_label} ({name}): Accidental horizontal overflow detected"
                        )

                    # Capture screenshot
                    screenshot_name = f"{page_label}-{name}"
                    screenshot_path = _capture_screenshot(page, screenshot_name)
                    print(f"  Screenshot: {screenshot_path}")

                    # Check for browser errors
                    assert not any(browser_issues.values()), (
                        f"{page_label} ({name}): Browser errors found: {browser_issues}"
                    )

                    # Quick actions / buttons present on business landing
                    if page_label == "business-landing":
                        quick_actions = page.locator(".quick-action")
                        if quick_actions.count() > 0:
                            expect(quick_actions.first).to_be_visible()

                    print(f"âœ“ {page_label} ({name}) ({width}x{height}): Passed")

                context.close()

            browser.close()
    finally:
        if flask_proc.poll() is None:
            flask_proc.terminate()
        try:
            flask_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            flask_proc.kill()
            flask_proc.wait(timeout=5)
        finally:
            try:
                app_temp_dir.cleanup()
            except PermissionError:
                pass
