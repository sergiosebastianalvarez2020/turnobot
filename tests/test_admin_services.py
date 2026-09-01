import re
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database


class TestAdminServices(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self.client = application.app.test_client()
        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

        login_page = self.client.get("/login")
        self.csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"', login_page.text
        ).group(1)
        self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": self.csrf_token},
        )

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def post_service(self, **values):
        data = {"csrf_token": self.csrf_token, "active": "1"}
        data.update(values)
        return self.client.post("/admin/servicios/guardar", data=data)

    def test_listar_servicios(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Corte", response.text)
        self.assertIn("Corte + barba", response.text)
        self.assertIn("Barba", response.text)

    def test_crear_servicio_valido(self):
        response = self.post_service(
            name="Masaje", price="12000", duration="45"
        )
        self.assertEqual(response.status_code, 302)
        services = database.get_all_services()
        service = next(row for row in services if row["name"] == "Masaje")
        self.assertEqual(service["price"], 12000)
        self.assertEqual(service["duration"], 45)

    def test_rechazar_nombre_vacio(self):
        before = len(database.get_all_services())
        self.post_service(name="", price="100", duration="30")
        self.assertEqual(len(database.get_all_services()), before)

    def test_rechazar_precio_invalido(self):
        before = len(database.get_all_services())
        self.post_service(name="Inválido", price="-1", duration="30")
        self.assertEqual(len(database.get_all_services()), before)

    def test_rechazar_duracion_invalida(self):
        before = len(database.get_all_services())
        self.post_service(name="Inválido", price="100", duration="0")
        self.assertEqual(len(database.get_all_services()), before)

    def test_editar_servicio(self):
        service = database.get_all_services()[0]
        response = self.post_service(
            service_id=str(service["id"]),
            name="Corte actualizado",
            price="11000",
            duration="35",
        )
        self.assertEqual(response.status_code, 302)
        updated = next(
            row for row in database.get_all_services() if row["id"] == service["id"]
        )
        self.assertEqual(updated["name"], "Corte actualizado")
        self.assertEqual(updated["price"], 11000)
        self.assertEqual(updated["duration"], 35)

    def test_activar_desactivar_servicio(self):
        service = database.get_all_services()[0]
        response = self.client.post(
            f"/admin/servicios/{service['id']}/estado",
            data={"csrf_token": self.csrf_token, "active": "0"},
        )
        self.assertEqual(response.status_code, 302)
        updated = next(
            row for row in database.get_all_services() if row["id"] == service["id"]
        )
        self.assertEqual(updated["active"], 0)


if __name__ == "__main__":
    unittest.main()
