"""Tests de contexto de negocio (tenant) migrados a PostgreSQL.

Estos tests validan el comportamiento de resolución de negocio (tenant) usando
la app fixture que proporciona una base de tests aislada por test.

Criterio de aislamiento:
- Tests que usan `self.client` (HTTP) no necesitan app_context explícito.
- Tests que llaman `tenant_module.load_current_business()` directamente
  usan `app_ctx` para tener un contexto aplicación activo.
- Tests que verifican ISOLAMIENTO entre requests crean su propio app_context
  por request para garantizar que flask.g no transporte estado.
- Para mocks de seams, se parchea sobre el módulo `app` (application),
  ya que _load_seam() resuelve desde sys.modules["app"].
"""

from unittest.mock import patch

import pytest
from flask import Flask

import app as application
import application.tenant as tenant_module
from tests._pg_compat import PostgreSQLTestCase


class TestBusinessContext(PostgreSQLTestCase):
    """Tests de tenant usando PostgreSQL aislado por test."""

    def test_request_normal_resuelve_el_corte(self, app_ctx):
        with app_ctx.test_request_context("/"):
            tenant_module.load_current_business()
            assert tenant_module.get_current_business_id() == 1

    def test_contexto_no_persiste_entre_requests(self, app):
        """g no debe transportar estado entre request contexts independientes.

        Cada app_context es independiente: g se limpia al salir del contexto
        de aplicación, garantizando aislamiento entre requests.
        """
        with app.app_context():
            with app.test_request_context("/"):
                tenant_module.load_current_business()
                assert tenant_module.get_current_business_id() == 1
        with app.app_context():
            with app.test_request_context("/"):
                from flask import g
                assert not hasattr(g, "current_business")
                assert tenant_module.get_current_business_id() is None

    def test_slug_existente_resuelve_id_1(self, app_ctx):
        business = tenant_module.resolve_business("el-corte")
        assert business["id"] == 1

    def test_slug_inexistente_devuelve_resultado_controlado(self, app_ctx):
        assert tenant_module.resolve_business("no-existe") is None

    def test_root_sigue_funcionando_y_mantiene_el_corte(self):
        response = self.client.get("/")
        assert response.status_code == 200
        # El client ya puso g en su request context; verificamos con uno nuevo
        with self.app.app_context():
            with self.app.test_request_context("/"):
                tenant_module.load_current_business()
                assert tenant_module.get_current_business_id() == 1
                from flask import g
                assert g.current_business["slug"] == "el-corte"

    def test_slug_route_resuelve_el_corte(self):
        response = self.client.get("/b/el-corte")
        assert response.status_code == 200

        with self.app.app_context():
            with self.app.test_request_context("/b/el-corte"):
                tenant_module.load_current_business()
                assert tenant_module.get_current_business_id() == 1
                from flask import g
                assert g.current_business["slug"] == "el-corte"

    def test_slug_inexistente_devuelve_404_y_no_fallback_al_corte(self):
        response = self.client.get("/b/slug-inexistente")
        assert response.status_code == 404

        with self.app.app_context():
            with self.app.test_request_context("/b/slug-inexistente"):
                with pytest.raises(Exception):
                    tenant_module.load_current_business()
                from flask import g
                assert not hasattr(g, "current_business")

    def test_slug_existente_de_negocio_b_usa_su_config_publica(self):
        """El resolve_business mockeado debe ser el que realmente usa la ruta.

        _load_seam() resuelve desde sys.modules["app"], así que el mock debe
        parchear en el módulo `application` (app facade), no en tenant_module.
        """
        business_b = {"id": 2, "name": "Business B", "slug": "business-b"}
        settings_b = {
            "business_name": "Business B",
            "business_type": "Barbería",
            "business_initials": "BB",
            "business_description": "Negocio de prueba",
            "timezone": "America/Argentina/Buenos_Aires",
            "slot_duration": 60,
            "break_between_slots": 0,
        }

        with patch.object(application, "resolve_business", return_value=business_b):
            with patch.object(application, "get_business_settings_scoped", return_value=settings_b):
                response = self.client.get("/b/business-b")
                assert response.status_code == 200
                body = response.get_data(as_text=True)
                assert "Business B" in body
                assert "Negocio de prueba" in body
                assert "BB" in body
                assert "El Corte" not in body

class TestBusinessContextSinBase:
    """Casos de contexto que no leen ni escriben persistencia."""

    def test_dos_requests_no_comparten_estado(self):
        """Dos requests consecutivos deben resolver negocios distintos sin
        compartir estado global."""
        app = Flask("business_context_unit")
        second_business = {"id": 2, "name": "Business B", "slug": "business-b"}

        with patch.object(
            application,
            "resolve_business",
            side_effect=[{"id": 1, "name": "El Corte", "slug": "el-corte"}, second_business],
        ):
            with app.test_request_context("/"):
                tenant_module.load_current_business()
                assert tenant_module.get_current_business_id() == 1
            with app.test_request_context("/"):
                tenant_module.load_current_business()
                assert tenant_module.get_current_business_id() == 2

    def test_no_hay_tenant_actual_en_variable_global_mutable(self):
        # La fachada y el módulo tenant no deben almacenar estado global.
        assert not hasattr(application, "current_business")
        assert not hasattr(application, "current_business_id")
        assert not hasattr(tenant_module, "current_business")
        assert not hasattr(tenant_module, "current_business_id")
