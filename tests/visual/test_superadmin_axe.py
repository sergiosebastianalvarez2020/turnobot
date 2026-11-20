"""Escaneo axe-core de /superadmin/identidad/1 sobre aplicación aislada.

Se ejecuta con el motor local `static/vendor/axe.min.js` y en modo WARN:
cualquier infracción preexistente se registra en JSON sin fallar el test.
"""

import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.no_pg

ROUTE = "/superadmin/identidad/1"
VIEWPORT_NAME = "390x844"


def _login_superadmin(page, base_url, email, password):
    page.goto(f"{base_url}/superadmin/login")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    with page.expect_navigation(timeout=20000):
        page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=15000)


def test_superadmin_identity_axe_scan(
    axe_scan, axe_asset_available, isolated_superadmin_app, superadmin_credentials
):
    """Verifica carga local de axe y genera el informe de la ruta Super Admin."""
    base_url = isolated_superadmin_app
    email, password = superadmin_credentials

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()

        _login_superadmin(page, base_url, email, password)
        assert "/superadmin/login" not in page.url, "No se alcanzó el panel Super Admin"

        response = page.goto(f"{base_url}{ROUTE}")
        assert response is not None and response.status == 200, f"HTTP inesperado en {ROUTE}"
        page.wait_for_load_state("networkidle", timeout=15000)

        report = axe_scan(page, route=ROUTE, viewport=VIEWPORT_NAME)
        assert report["axeVersion"], "axe-core no reportó versión"
        assert isinstance(report["violations"], list)
        assert isinstance(report["incomplete"], list)
        assert isinstance(report["passesCount"], int)

        context.close()
        browser.close()
