"""Etapa 3: experiencia cliente/bot con recursos.

Cubre:
- API pública de recursos
- Disponibilidad filtrada por recurso
- Reserva pública con resource_id opcional
- Aislamiento multi-tenant
- Negocios sin recursos
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app as application
import database.database as database
from database.database import (
    create_resource_scoped,
    get_connection,
    update_business_settings_scoped,
)


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class BaseResourcePublicAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_database_path = database.DATABASE_PATH

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.date = _next_open_day()
        self._setup_businesses()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _setup_businesses(self):
        # Business 1 (El Corte) ya lo crea init_database() via migración 003
        # Solo creamos Business 2 (Pádel) y sus datos
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO businesses (id, name, slug)
                VALUES (2, 'Padel Club', 'padel-club')
            """)
            conn.execute("""
                INSERT INTO services (id, business_id, name, price, duration, active)
                VALUES (10, 2, 'Partido Pádel', 15000, 90, 1)
            """)
            conn.execute("""
                INSERT INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end, business_id)
                VALUES
                    (0, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (1, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (2, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (3, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (4, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (5, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (6, 0, NULL, NULL, NULL, NULL, 2)
            """)
            conn.commit()
        finally:
            conn.close()

        update_business_settings_scoped(
            1,
            "El Corte",
            "Barbería",
            "EC",
            "Corte clásico",
            "America/Argentina/Buenos_Aires",
            notifications_enabled=0,
        )
        update_business_settings_scoped(
            2,
            "Padel Club",
            "Pádel",
            "PC",
            "Club de pádel",
            "America/Argentina/Buenos_Aires",
            notifications_enabled=0,
        )

    def _create_resource(self, business_id, name, active=True):
        return create_resource_scoped(business_id, name, active=active)

    @staticmethod
    def _query(sql, params=None):
        connection = get_connection()
        try:
            return connection.execute(sql, params or ()).fetchall()
        finally:
            connection.close()


class TestPublicResourcesAPI(BaseResourcePublicAPITest):
    """Tests para la API pública de recursos."""

    def test_negocio_con_recursos_devuelve_sus_recursos(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")
        self._create_resource(2, "Cancha 2")
        self._create_resource(2, "Cancha 3")
        self._create_resource(2, "Cancha 4")

        resp = client.get("/b/padel-club/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert len(data["recursos"]) == 4
        nombres = {r["nombre"] for r in data["recursos"]}
        assert nombres == {"Cancha 1", "Cancha 2", "Cancha 3", "Cancha 4"}

    def test_negocio_sin_recursos_devuelve_lista_vacia(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        resp = client.get("/b/el-corte/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["recursos"] == []

    def test_recurso_inactivo_no_aparece_publicamente(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha Activa")
        self._create_resource(2, "Cancha Inactiva", active=False)

        resp = client.get("/b/padel-club/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["recursos"]) == 1
        assert data["recursos"][0]["nombre"] == "Cancha Activa"

    def test_tenant_a_no_ve_recursos_de_tenant_b(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        resp = client.get("/b/el-corte/api/recursos")
        data = json.loads(resp.data)
        assert data["recursos"] == []

    def test_rate_limiting_recursos(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        for _ in range(70):
            client.get("/b/padel-club/api/recursos")

        resp = client.get("/b/padel-club/api/recursos")
        assert resp.status_code in (200, 429)


class TestAvailabilityWithResource(BaseResourcePublicAPITest):
    """Tests para disponibilidad filtrada por recurso."""

    def test_disponibilidad_sin_recurso_negocio_con_recursos(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")
        self._create_resource(2, "Cancha 2")

        resp = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}?servicio=Partido%20P%C3%A1del"
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert "horarios_disponibles" in data

    def test_disponibilidad_con_resource_id_valido(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        court1 = self._create_resource(2, "Cancha 1")

        resp = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}"
            f"?servicio=Partido%20P%C3%A1del&resource_id={court1}"
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert "horarios_disponibles" in data

    def test_disponibilidad_resource_id_inexistente(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        resp = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}"
            f"?servicio=Partido%20P%C3%A1del&resource_id=999"
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["horarios_disponibles"] == []

    def test_disponibilidad_resource_id_invalido(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        resp = client.get(f"/b/padel-club/api/disponibilidad/{self.date}?resource_id=abc")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "resource_id inválido" in data["error"]

    def test_disponibilidad_resource_id_de_otro_tenant(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        resp = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}"
            f"?servicio=Partido%20P%C3%A1del&resource_id=999"
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["horarios_disponibles"] == []

    def test_dos_canchas_diferentes_mismo_horario_disponible(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")
        self._create_resource(2, "Cancha 2")

        resp1 = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}"
            f"?servicio=Partido%20P%C3%A1del&resource_id=1"
        )
        resp2 = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}"
            f"?servicio=Partido%20P%C3%A1del&resource_id=2"
        )
        data1 = json.loads(resp1.data)
        data2 = json.loads(resp2.data)
        assert data1["horarios_disponibles"] == data2["horarios_disponibles"]
        assert len(data1["horarios_disponibles"]) > 0


class TestPublicReservationWithResource(BaseResourcePublicAPITest):
    """Tests para reserva pública con resource_id."""

    def test_reserva_publica_con_recurso_valido(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        court1 = self._create_resource(2, "Cancha 1")

        payload = {
            "nombre": "Juan Pérez",
            "telefono": "1122334455",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "10:00",
            "resource_id": court1,
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 201
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["resource_id"] == court1
        assert data["resource_nombre"] == "Cancha 1"

    def test_reserva_publica_sin_recurso_mantiene_comportamiento_anterior(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        payload = {
            "nombre": "María López",
            "telefono": "1133445566",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "11:00",
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 201
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data.get("resource_id") is None

    def test_reserva_con_resource_id_inexistente_rechazada(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        payload = {
            "nombre": "Pedro García",
            "telefono": "1144556677",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "12:00",
            "resource_id": 999,
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False

    def test_reserva_con_resource_id_inactivo_rechazada(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        court5 = self._create_resource(2, "Cancha Mantenimiento", active=False)

        payload = {
            "nombre": "Ana Ruiz",
            "telefono": "1155667788",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "13:00",
            "resource_id": court5,
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False

    def test_reserva_con_resource_id_de_otro_tenant_rechazada(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        resp = client.post(
            "/b/padel-club/api/reservar",
            data=json.dumps(
                {
                    "nombre": "Carlos Díaz",
                    "telefono": "1166778899",
                    "servicio": "Partido Pádel",
                    "fecha": self.date,
                    "hora": "14:00",
                    "resource_id": 9999,
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False

    def test_misma_cancha_no_se_puede_duplicar(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        court1 = self._create_resource(2, "Cancha 1")

        payload1 = {
            "nombre": "Cliente 1",
            "telefono": "1111111111",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "15:00",
            "resource_id": court1,
        }
        resp1 = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload1), content_type="application/json"
        )
        assert resp1.status_code == 201

        payload2 = {
            "nombre": "Cliente 2",
            "telefono": "2222222222",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "15:00",
            "resource_id": court1,
        }
        resp2 = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload2), content_type="application/json"
        )
        assert resp2.status_code == 400
        data = json.loads(resp2.data)
        assert data["reason"] == "occupied"

    def test_dos_canchas_diferentes_mismo_horario_permitido(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        court1 = self._create_resource(2, "Cancha 1")
        court2 = self._create_resource(2, "Cancha 2")

        base_payload = {"servicio": "Partido Pádel", "fecha": self.date, "hora": "16:00"}

        payload1 = {
            **base_payload,
            "nombre": "Jugador 1",
            "telefono": "1111111111",
            "resource_id": court1,
        }
        resp1 = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload1), content_type="application/json"
        )
        assert resp1.status_code == 201

        payload2 = {
            **base_payload,
            "nombre": "Jugador 2",
            "telefono": "2222222222",
            "resource_id": court2,
        }
        resp2 = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload2), content_type="application/json"
        )
        assert resp2.status_code == 201

    def test_bloqueo_global_sigue_bloqueando_recursos(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        payload_global = {
            "nombre": "Reserva Global",
            "telefono": "9999999999",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "17:00",
        }
        resp_global = client.post(
            "/b/padel-club/api/reservar",
            data=json.dumps(payload_global),
            content_type="application/json",
        )
        assert resp_global.status_code == 201

        payload_cancha = {
            "nombre": "Jugador Cancha",
            "telefono": "8888888888",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "17:00",
            "resource_id": 1,
        }
        resp_cancha = client.post(
            "/b/padel-club/api/reservar",
            data=json.dumps(payload_cancha),
            content_type="application/json",
        )
        assert resp_cancha.status_code == 400
        data = json.loads(resp_cancha.data)
        assert data["reason"] == "occupied"

    def test_reserva_resource_id_invalido_tipo(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        payload = {
            "nombre": "Test",
            "telefono": "1111111111",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "18:00",
            "resource_id": "abc",
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "resource_id inválido" in data["error"]


class TestAislamientoMultiTenant(BaseResourcePublicAPITest):
    """Tests de aislamiento multi-tenant para APIs públicas."""

    def test_api_recursos_aislada_por_tenant(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")
        self._create_resource(2, "Cancha 2")
        self._create_resource(2, "Cancha 3")
        self._create_resource(2, "Cancha 4")

        resp1 = client.get("/b/el-corte/api/recursos")
        resp2 = client.get("/b/padel-club/api/recursos")
        data1 = json.loads(resp1.data)
        data2 = json.loads(resp2.data)
        assert data1["recursos"] == []
        assert len(data2["recursos"]) == 4

    def test_api_disponibilidad_aislada_por_tenant(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        self._create_resource(2, "Cancha 1")

        resp = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}?servicio=Partido%20P%C3%A1del"
        )
        data = json.loads(resp.data)
        assert len(data["horarios_disponibles"]) > 0

    def test_api_reserva_aislada_por_tenant(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        court1 = self._create_resource(2, "Cancha 1")

        payload = {
            "nombre": "Jugador Pádel",
            "telefono": "2222222222",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "10:00",
            "resource_id": court1,
        }
        client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )

        resp = client.get(f"/b/el-corte/api/disponibilidad/{self.date}?servicio=Corte")
        data = json.loads(resp.data)
        assert len(data["horarios_disponibles"]) > 0


class TestNegocioSinRecursos(BaseResourcePublicAPITest):
    """Tests para negocios que no usan recursos."""

    def test_reserva_sin_recursos_funciona_normal(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        payload = {
            "nombre": "Cliente Barbería",
            "telefono": "1177889900",
            "servicio": "Corte",
            "fecha": self.date,
            "hora": "10:00",
        }
        resp = client.post(
            "/b/el-corte/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        assert resp.status_code == 201
        data = json.loads(resp.data)
        assert data["success"] is True

    def test_disponibilidad_sin_recursos_sin_filtro(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        resp = client.get(f"/b/el-corte/api/disponibilidad/{self.date}?servicio=Corte")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert len(data["horarios_disponibles"]) > 0

    def test_api_recursos_devuelve_lista_vacia(self):
        from app import app as flask_app

        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        resp = client.get("/b/el-corte/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["recursos"] == []


if __name__ == "__main__":
    unittest.main()
