"""Visual responsive tests for the TurnoBot admin panel using Playwright.

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

TEST_ADMIN_EMAIL = "owner@test.local"
TEST_ADMIN_PASSWORD = "testpass123"

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def _capture_screenshot(page, name: str) -> Path:
    """Capture a screenshot and return the path."""
    path = RESULTS_DIR / f"{name}.png"
    page.screenshot(path=path, full_page=True)
    return path


def _has_horizontal_overflow(page) -> bool:
    """Detect if document has accidental horizontal overflow (ignore intentional table scroll)."""
    result = page.evaluate(
        """() => {
      const body = document.body;
      const html = document.documentElement;
      const viewportWidth = Math.max(window.innerWidth, html.clientWidth);
      const docWidth = Math.max(body.scrollWidth, html.scrollWidth, body.offsetWidth, html.offsetWidth);
      // Allow small rounding differences
      if (docWidth > viewportWidth + 1) {
        // Check if all overflowing width is inside intentional scroll containers
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
        return false;
      }
      // Also check for elements positioned outside viewport
      const elements = document.querySelectorAll('*');
      for (const el of elements) {
        if (el.closest('.table-container, .smtp-instructions')) continue; // intentional internal table scroll
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.right > viewportWidth + 2) {
          const style = window.getComputedStyle(el);
          if (style.position === 'fixed' || style.position === 'absolute') {
            // check if it's intentional (like modals/overlays)
            continue;
          }
          // Check if it's a child of scrollable
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


def _overflow_diagnostics(page) -> dict:
    """Return document metrics and visible out-of-viewport elements."""
    return page.evaluate(
        """() => {
      const viewportWidth = document.documentElement.clientWidth;
      const describe = (el) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const parent = el.parentElement;
        const parentRect = parent?.getBoundingClientRect();
        const selector = el.id ? `#${el.id}` :
          `${el.tagName.toLowerCase()}${[...el.classList].map(c => `.${c}`).join('')}`;
        return {
          selector, tag: el.tagName.toLowerCase(), classes: [...el.classList],
          left: +rect.left.toFixed(2), right: +rect.right.toFixed(2),
          width: +rect.width.toFixed(2), parentWidth: parentRect ? +parentRect.width.toFixed(2) : null,
          scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
          overflowX: style.overflowX, minWidth: style.minWidth, maxWidth: style.maxWidth,
          position: style.position,
          parentSelector: parent ? (parent.id ? `#${parent.id}` : `${parent.tagName.toLowerCase()}${[...parent.classList].map(c => `.${c}`).join('')}`) : null
        };
      };
      const visible = [...document.querySelectorAll('body *')].filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
      });
      const outside = visible.filter(el => {
        const r = el.getBoundingClientRect();
        return r.right > viewportWidth + 1 || r.left < -1;
      });
      const scrollSelector = '.table-container, .smtp-instructions, [data-scroll-container]';
      const insideScrollContainer = el => Boolean(el.closest(scrollSelector));
      const outsideSet = new Set(outside);
      const primaryOutside = outside.filter(el => {
        let parent = el.parentElement;
        while (parent) {
          if (outsideSet.has(parent)) return false;
          parent = parent.parentElement;
        }
        return true;
      });
      const positioned = visible.filter(el => ['fixed', 'absolute'].includes(getComputedStyle(el).position));
      const pseudo = [];
      for (const el of visible) for (const pseudoName of ['::before', '::after']) {
        const style = getComputedStyle(el, pseudoName);
        if (style.content !== 'none' && style.content !== 'normal') {
          pseudo.push({ ...describe(el), pseudo: pseudoName, content: style.content,
            pseudoWidth: style.width, pseudoPosition: style.position, pseudoTransform: style.transform });
        }
      }
      const original = {
        html: document.documentElement.getAttribute('style'),
        body: document.body.getAttribute('style'),
        containers: [...document.querySelectorAll('.admin-container, .main-content')].map(el => [el, el.getAttribute('style')])
      };
      for (const el of [document.documentElement, document.body, ...document.querySelectorAll('.admin-container, .main-content')]) {
        el.style.setProperty('overflow-x', 'visible', 'important');
      }
      const unclippedDocument = {
        htmlScrollWidth: document.documentElement.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth
      };
      if (original.html === null) document.documentElement.removeAttribute('style'); else document.documentElement.setAttribute('style', original.html);
      if (original.body === null) document.body.removeAttribute('style'); else document.body.setAttribute('style', original.body);
      for (const [el, style] of original.containers) {
        if (style === null) el.removeAttribute('style'); else el.setAttribute('style', style);
      }
      return {
        viewportWidth,
        document: {
          htmlScrollWidth: document.documentElement.scrollWidth,
          htmlClientWidth: document.documentElement.clientWidth,
          bodyScrollWidth: document.body.scrollWidth,
          bodyClientWidth: document.body.clientWidth,
          innerWidth: window.innerWidth
        },
        unclippedDocument,
        outside: primaryOutside.filter(el => !insideScrollContainer(el) &&
          !['fixed', 'absolute'].includes(getComputedStyle(el).position)).map(describe),
        intentionalScrollOverflow: primaryOutside.filter(insideScrollContainer).map(describe),
        scrollContainers: [...document.querySelectorAll(scrollSelector)].map(el => ({
          ...describe(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth
        })),
        positioned: [...new Map(positioned.map(el => [
          `${el.tagName.toLowerCase()}${[...el.classList].map(c => `.${c}`).join('')}`,
          { selector: el.id ? `#${el.id}` : `${el.tagName.toLowerCase()}${[...el.classList].map(c => `.${c}`).join('')}`,
            count: positioned.filter(item => item.tagName === el.tagName && item.className === el.className).length,
            positions: positioned.filter(item => item.tagName === el.tagName && item.className === el.className).map(item => {
              const r = item.getBoundingClientRect();
              return { left: +r.left.toFixed(2), right: +r.right.toFixed(2), width: +r.width.toFixed(2) };
            }) }
        ])).values()], pseudo
      };
    }"""
    )


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


def _assert_sidebar_and_navigation(page, width: int, name: str) -> None:
    """Exercise the sidebar and bottom navigation at responsive breakpoints."""
    sidebar = page.locator("#sidebar")
    toggle = page.locator("#sidebar-toggle")
    bottom_nav = page.locator("nav.bottom-nav[aria-label='Navegación móvil']")

    if width > 850:
        collapse = page.locator("#sidebar .sidebar-collapse-btn")
        expect(collapse).to_be_visible()
        assert "sidebar-open" not in sidebar.get_attribute("class").split()
        initial_width = sidebar.evaluate("element => element.getBoundingClientRect().width")
        collapse.click()
        assert "collapsed" in sidebar.get_attribute("class").split()
        expect(sidebar).to_have_css("width", "64px")
        collapse.click()
        assert "collapsed" not in sidebar.get_attribute("class").split()
        assert sidebar.evaluate("element => element.getBoundingClientRect().width") == initial_width
        expect(toggle).to_be_hidden()
    else:
        expect(toggle).to_be_visible()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        assert "sidebar-open" not in sidebar.get_attribute("class").split()
        assert sidebar.evaluate("element => element.getBoundingClientRect().left") < 0
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        assert "sidebar-open" in sidebar.get_attribute("class").split()
        assert "overlay-visible" in page.locator("#sidebar-overlay").get_attribute("class").split()
        page.wait_for_timeout(300)
        assert sidebar.evaluate("element => element.getBoundingClientRect().left") == 0
        page.locator("#sidebar-overlay").click(position={"x": width - 10, "y": 10})
        expect(toggle).to_have_attribute("aria-expanded", "false")
        assert "sidebar-open" not in sidebar.get_attribute("class").split()
        page.wait_for_timeout(300)
        assert sidebar.evaluate("element => element.getBoundingClientRect().left") < 0

    if width <= 768:
        expect(bottom_nav).to_be_visible()
        items = bottom_nav.locator("a.bottom-nav-item")
        expect(items).to_have_count(5)
        expected = [
            ("Inicio", "/admin"),
            ("Agenda", "/admin#agenda"),
            ("Turnos", "/admin"),
            ("Clientes", "/admin/usuarios"),
            ("Más", "#"),
        ]
        actual = [
            (item.locator("small").inner_text(), item.get_attribute("href")) for item in items.all()
        ]
        assert actual == expected, f"{name}: unexpected bottom navigation: {actual}"
        assert items.nth(0).get_attribute("class") and "active" in items.nth(0).get_attribute(
            "class"
        )
        items.nth(1).click()
        assert page.url.endswith("#agenda"), f"{name}: Agenda did not navigate to #agenda"
    else:
        expect(bottom_nav).to_be_hidden()


def _assert_responsive_breakpoints(page, original_width: int, original_height: int) -> None:
    """Verify the CSS and navigation mode at the 768, 850, and 851 px edges."""
    for width in (768, 850, 851):
        page.set_viewport_size({"width": width, "height": original_height})
        toggle = page.locator("#sidebar-toggle")
        sidebar = page.locator("#sidebar")
        bottom_nav = page.locator("nav.bottom-nav[aria-label='Navegación móvil']")
        page.wait_for_timeout(300)
        sidebar_left = sidebar.evaluate("element => element.getBoundingClientRect().left")

        if width <= 850:
            expect(toggle).to_be_visible()
            assert sidebar_left < 0, f"Sidebar should be closed at {width}px"
        else:
            expect(toggle).to_be_hidden()
            assert sidebar_left == 0, f"Desktop sidebar should be visible at {width}px"

        if width <= 768:
            expect(bottom_nav).to_be_visible()
        else:
            expect(bottom_nav).to_be_hidden()

    page.set_viewport_size({"width": original_width, "height": original_height})


def _start_flask_app():
    """Start an isolated Flask app with its SQLite database in a temp directory."""
    project_root = Path(__file__).resolve().parents[2]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    app_temp_dir = tempfile.TemporaryDirectory(prefix="turnobot-visual-")
    temp_root = Path(app_temp_dir.name).resolve()
    database_path = (temp_root / "appointments.sqlite3").resolve()
    default_database_path = (project_root / "database" / "appointments.db").resolve()
    if database_path == default_database_path:
        app_temp_dir.cleanup()
        raise RuntimeError("Visual test database must not use the normal TurnoBot database")

    # Whitelist only OS environment needed to launch Python. Application,
    # database, service, and authentication settings below are test-local.
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
            "ADMIN_TEST_EMAIL": TEST_ADMIN_EMAIL,
            "ADMIN_TEST_PASSWORD": TEST_ADMIN_PASSWORD,
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

# Import only the database module, redirect it, then import/create Flask.
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

# create_app() performs the single normal SQLite initialization/migration pass
# after DATABASE_PATH has been redirected to the disposable database.
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


def _login_if_needed(page, base_url):
    """Log in with the test-only account created in the isolated database."""
    page.goto(f"{base_url}/login")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.fill('input[name="email"]', TEST_ADMIN_EMAIL)
    page.fill('input[name="password"]', TEST_ADMIN_PASSWORD)
    with page.expect_navigation(timeout=20000):
        page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=15000)


@pytest.mark.no_pg
def test_admin_responsive_visual():
    """Test the admin panel responsiveness and visual integrity.

    Runs against all three resolutions: desktop, tablet, mobile.
    This test:
    - Uses create_app() for an isolated Flask instance
    - Does NOT start or modify the production application
    - Does NOT access AWS or production servers
    - Uses only fixed test credentials created in the isolated database
    - Does NOT contain real credentials, tokens, or secrets
    - Does not read credentials from the parent process environment
    - Verifies user actually reached the admin panel (not just login form)
    - Checks for horizontal overflow
    - Captures console errors
    - Captures screenshots at each resolution
    - Validates sidebar behavior

    The test is designed to be run in isolation without PostgreSQL.
    The `no_pg` marker ensures it's excluded from PG test suites.
    """
    # Start Flask app in subprocess (development only)
    flask_proc, app_temp_dir, base_url = _start_flask_app()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            for resolution in RESOLUTIONS:
                name = resolution["name"]
                width = resolution["width"]
                height = resolution["height"]

                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()

                browser_issues = _capture_browser_issues(page)

                _login_if_needed(page, base_url)
                if "/admin" not in page.url:
                    page.goto(f"{base_url}/admin")
                    page.wait_for_load_state("networkidle", timeout=15000)

                # Verify we're on the admin panel (not login page)
                is_admin_page = page.evaluate(
                    """() => {
      const title = document.title.toLowerCase();
      const hasAdminTitle = title.includes('admin');
      const sidebar = document.querySelector('.sidebar, .admin-sidebar');
      const hasSidebar = sidebar && sidebar.style.display !== 'none';
      return hasAdminTitle || hasSidebar;
    }"""
                )

                assert is_admin_page, f"{name}: Not on admin panel (possibly at login page)"

                _assert_sidebar_and_navigation(page, width, name)
                _assert_responsive_breakpoints(page, width, height)

                # Check for horizontal overflow
                has_overflow = _has_horizontal_overflow(page)
                if name == "mobile":
                    print(f"  Mobile overflow diagnostics: {_overflow_diagnostics(page)}")

                # Capture screenshot
                screenshot_path = _capture_screenshot(page, name)

                # Check sidebar is visible (not collapsed off-screen)
                sidebar_visible = page.evaluate(
                    """() => {
      const sidebar = document.querySelector('.sidebar, .admin-sidebar, [role="navigation"]');
      if (!sidebar) return false;
      const style = window.getComputedStyle(sidebar);
      return style.display !== 'none';
    }"""
                )

                # Assertions
                assert not any(browser_issues.values()), (
                    f"{name}: Browser errors found: {browser_issues}"
                )

                if has_overflow:
                    print(f"  WARNING: {name}: Horizontal overflow detected")
                else:
                    print(
                        f"  {name}: no accidental document overflow (internal table scrolling is allowed)"
                    )

                assert not has_overflow, f"{name}: Accidental horizontal overflow detected"

                assert sidebar_visible, f"{name}: Sidebar not visible"

                print(f"âœ“ {name} ({width}Ã—{height}): Passed")
                print(f"  Screenshot: {screenshot_path}")
                if browser_issues:
                    print(f"  Browser issues: {browser_issues}")

                context.close()

            browser.close()
    finally:
        # Stop the subprocess before deleting its disposable database directory.
        try:
            if flask_proc.poll() is None:
                flask_proc.terminate()
            try:
                flask_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                flask_proc.kill()
                flask_proc.wait(timeout=5)
        finally:
            app_temp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "--tb=short", __file__]))
