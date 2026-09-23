"""
Regression tests for load_current_business() before_request hook.

Bug original: cuando request.path empieza con /b/ pero la URL no hace match
con ninguna ruta registrada (p.ej. /b/<slug>/admin/login), Flask setea
request.view_args = None. El código anterior llamaba request.view_args.get("slug")
directamente, causando AttributeError: 'NoneType' object has no attribute 'get'.

La corrección retorna 404 de forma segura en ese caso, sin fallback cross-tenant.
"""

import unittest

import app as application


class TestLoadCurrentBusinessRegression(unittest.TestCase):
    """
    Regresión: load_current_business no debe propagar AttributeError
    cuando request.view_args es None (ruta /b/... sin match).
    """

    def test_ruta_b_slug_admin_login_devuelve_404_sin_crashear(self):
        """
        /b/<slug>/admin/login no existe como ruta registrada.
        Antes del fix → HTTP 500 (AttributeError).
        Después del fix → HTTP 404.
        """
        with application.app.test_client() as client:
            response = client.get("/b/el-corte/admin/login")
            self.assertEqual(
                response.status_code,
                404,
                "Una ruta /b/<slug>/... sin match debe devolver 404, nunca 500",
            )

    def test_ruta_b_slug_inexistente_subpath_devuelve_404(self):
        """
        /b/<slug>/ruta/que/no/existe tampoco debe crashear.
        """
        with application.app.test_client() as client:
            response = client.get("/b/el-corte/ruta/inexistente/profunda")
            self.assertEqual(response.status_code, 404)

    def test_ruta_b_prefijo_sin_slug_devuelve_404(self):
        """
        Una URL que empieza con /b/ pero no tiene slug tampoco debe crashear.
        Ejemplo: /b/ sola.
        """
        with application.app.test_client() as client:
            response = client.get("/b/")
            # Puede ser 404 o 405 dependiendo de cómo Flask haga el routing,
            # pero NUNCA debe ser 500.
            self.assertNotEqual(
                response.status_code, 500, "Ninguna ruta bajo /b/ sin match debe devolver 500"
            )

    def test_ruta_b_slug_existente_login_funciona_correctamente(self):
        """
        /b/el-corte/login (ruta que SÍ existe) debe devolver 200.
        Verificación de que el fix no rompe rutas válidas con slug.
        """
        with application.app.test_client() as client:
            response = client.get("/b/el-corte/login")
            self.assertEqual(
                response.status_code,
                200,
                "/b/<slug>/login es una ruta válida y debe seguir respondiendo 200",
            )

    def test_ruta_b_slug_existente_admin_funciona_correctamente(self):
        """
        /b/el-corte/admin (ruta que SÍ existe) debe redirigir al login,
        no crashear ni devolver 500.
        """
        with application.app.test_client() as client:
            response = client.get("/b/el-corte/admin")
            self.assertIn(
                response.status_code,
                [200, 302, 401],
                "/b/<slug>/admin debe responder con un código válido, nunca 500",
            )
            self.assertNotEqual(response.status_code, 500)

    def test_view_args_none_no_propaga_exception(self):
        """
        Test directo a load_current_business con view_args=None.
        Simula exactamente la condición que causaba el AttributeError.
        No debe propagar ninguna excepción.
        """
        with application.app.test_request_context("/b/el-corte/admin/login"):
            # Simular Flask sin match de ruta: view_args = None
            application.request.view_args = None
            try:
                # Debe retornar un Response (abort 404), no lanzar excepción
                application.load_current_business()
                # Si llegamos acá sin excepción, el fix funciona correctamente.
                # El resultado puede ser None (si el path no empieza con /b/)
                # o un Response 404.
            except AttributeError as exc:
                self.fail(f"load_current_business propagó AttributeError con view_args=None: {exc}")
            except Exception:
                # Abort lanza werkzeug.exceptions.NotFound, que es el comportamiento correcto.
                pass

    def test_rutas_sin_b_prefix_no_son_afectadas(self):
        """
        Rutas normales (/, /health, /login) deben seguir funcionando.
        El fix no debe impactar la rama else de load_current_business.
        """
        with application.app.test_client() as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)

            response = client.get("/")
            self.assertEqual(response.status_code, 200)

    def test_slug_invalido_en_ruta_existente_devuelve_404(self):
        """
        /b/slug-que-no-existe/login → la ruta /b/<slug>/login sí existe,
        pero el slug no resuelve a ningún negocio → debe ser 404.
        """
        with application.app.test_client() as client:
            response = client.get("/b/negocio-fantasma/login")
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
