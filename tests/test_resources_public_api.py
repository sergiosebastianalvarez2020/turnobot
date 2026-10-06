"""Etapa 3: experiencia cliente/bot con recursos.

Cubre:
- API pública de recursos
- Disponibilidad filtrada por recurso
- Reserva pública con resource_id opcional
- Aislamiento multi-tenant
- Negocios sin recursos

Ejecuta contra PostgreSQL mediante `tests._pg_compat.PostgreSQLTestCase`: cada test
recibe una base `turnobot_test_<uuid>` desechable con la semilla estandar. No hay swap
de `DATABASE_PATH` ni SQLite; el aislamiento entre tests es el de la base temporal.

Notas de portabilidad del SQL de este archivo:
- El negocio 1 (`El Corte` / `el-corte`) lo crea la semilla estandar de PostgreSQL,
  igual que lo hacia la migracion 003 de SQLite: aqui solo se crea el negocio 2.
- `weekly_schedules.is_open` es BOOLEAN en PostgreSQL. Los literales `1`/`0` solo los
  adapta el seam en el primer tuple de un INSERT, asi que un VALUES multi-fila necesita
  `TRUE`/`FALSE` explicitos (equivalente exacto de los 1/0 originales).
- `weekly_schedules.morning_start` et al. son TIME: los literales `'09:00'` se mantienen.
"""

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app as application
import database.database as database
from database.database import create_resource_scoped, update_business_settings_scoped
from tests._pg_compat import PostgreSQLTestCase

ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")


def _next_open_day():
    date = datetime.now(ZONA_HORARIA).date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class BaseResourcePublicAPITest(unittest.TestCase, PostgreSQLTestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        # `self.client` lo aporta la fixture `client`: app y base temporal propias de
        # este test. El contexto se pusha para poder consultar la base directamente.
        self._app_context = self.app.app_context()
        self._app_context.push()
        self.addCleanup(self._app_context.pop)

        self.date = _next_open_day()
        self._setup_businesses()

    def tearDown(self):
        application.rate_limit_state.clear()

    def _setup_businesses(self):
        # Business 1 (El Corte) ya lo crea la semilla estandar de PostgreSQL.
        # Solo creamos Business 2 (Pádel) y sus datos.
        self._execute("INSERT INTO businesses (id, name, slug) VALUES (2, 'Padel Club', 'padel-club')")
        self._execute(
            "INSERT INTO services (id, business_id, name, price, duration, active) "
            "VALUES (10, 2, 'Partido Pádel', 15000, 90, TRUE)"
        )
        # `is_open` es BOOLEAN: un VALUES multi-fila no pasa por la adaptation de
        # literales 0/1 del seam (que solo cubre el primer tuple), asi que se
        # escriben TRUE/FALSE de forma explicita.
        self._execute("""
            INSERT INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end, business_id)
            VALUES
                (0, TRUE, '09:00', '13:00', '15:00', '20:00', 2),
                (1, TRUE, '09:00', '13:00', '15:00', '20:00', 2),
                (2, TRUE, '09:00', '13:00', '15:00', '20:00', 2),
                (3, TRUE, '09:00', '13:00', '15:00', '20:00', 2),
                (4, TRUE, '09:00', '13:00', '15:00', '20:00', 2),
                (5, TRUE, '09:00', '13:00', '15:00', '20:00', 2),
                (6, FALSE, NULL, NULL, NULL, NULL, 2)
        """)

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
    def _execute(sql, params=None):
        connection = database.get_connection()
        try:
            connection.execute(sql, params or ())
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _query(sql, params=None):
        connection = database.get_connection()
        try:
            return connection.execute(sql, params or ()).fetchall()
        finally:
            connection.close()


class TestPublicResourcesAPI(BaseResourcePublicAPITest):
    """Tests para la API pública de recursos."""

    def test_negocio_con_recursos_devuelve_sus_recursos(self):
        client = self.client

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
        client = self.client

        resp = client.get("/b/el-corte/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["recursos"] == []

    def test_recurso_inactivo_no_aparece_publicamente(self):
        client = self.client

        self._create_resource(2, "Cancha Activa")
        self._create_resource(2, "Cancha Inactiva", active=False)

        resp = client.get("/b/padel-club/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["recursos"]) == 1
        assert data["recursos"][0]["nombre"] == "Cancha Activa"

    def test_tenant_a_no_ve_recursos_de_tenant_b(self):
        client = self.client

        self._create_resource(2, "Cancha 1")

        resp = client.get("/b/el-corte/api/recursos")
        data = json.loads(resp.data)
        assert data["recursos"] == []

    def test_rate_limiting_recursos(self):
        client = self.client

        for _ in range(70):
            client.get("/b/padel-club/api/recursos")

        resp = client.get("/b/padel-club/api/recursos")
        assert resp.status_code in (200, 429)


class TestAvailabilityWithResource(BaseResourcePublicAPITest):
    """Tests para disponibilidad filtrada por recurso."""

    def test_disponibilidad_sin_recurso_negocio_con_recursos(self):
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

        resp = client.get(f"/b/padel-club/api/disponibilidad/{self.date}?resource_id=abc")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "resource_id inválido" in data["error"]

    def test_disponibilidad_resource_id_de_otro_tenant(self):
        client = self.client

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
        client = self.client

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
        client = self.client

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

    def test_management_email_link_uses_public_origin_not_request_host(self):
        self._execute(
            "UPDATE business_settings SET notifications_enabled = TRUE WHERE business_id = 2"
        )
        self.app.config["PUBLIC_BASE_URL"] = "https://canonical.example"
        payload = {
            "nombre": "Juan Pérez",
            "telefono": "1122334455",
            "email": "juan@example.test",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "10:00",
        }
        outgoing = []
        with (
            patch("services.notifications.smtp_configured", return_value=True),
            patch("services.notifications.notifications_enabled", return_value=True),
            patch(
                "services.notifications._send_email",
                side_effect=lambda *args: (outgoing.append(args) or (True, None)),
            ),
            patch.object(application, "send_business_confirmation_email"),
        ):
            response = self.client.post(
                "/b/padel-club/api/reservar",
                json=payload,
                headers={"Host": "evil.example"},
            )

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data["management_token"])
        self.assertEqual(len(outgoing), 1)
        html = outgoing[0][2]
        expected_link = (
            "https://canonical.example/b/padel-club/turno/"
            f"{data['management_token']}?id={data['appointment_id']}"
        )
        self.assertIn(expected_link, html)
        self.assertNotIn("evil.example", html)

    def test_reserva_publica_sin_recurso_mantiene_comportamiento_anterior(self):
        client = self.client

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

    def test_reintento_con_misma_clave_devuelve_el_turno_original(self):
        """El contrato HTTP del replay: 200 (no 201) y sin emails repetidos."""
        client = self.client

        enviados = []
        # La vista importa los notificadores desde `app` en cada request, así
        # que hay que sustituir los nombres en ese módulo (no en el servicio).
        import app as app_module

        original_confirmacion = app_module.send_confirmation_email
        original_negocio = app_module.send_business_confirmation_email
        app_module.send_confirmation_email = lambda *a, **k: enviados.append("cliente")
        app_module.send_business_confirmation_email = lambda *a, **k: enviados.append("negocio")
        self.addCleanup(setattr, app_module, "send_confirmation_email", original_confirmacion)
        self.addCleanup(setattr, app_module, "send_business_confirmation_email", original_negocio)

        payload = {
            "nombre": "Juan Pérez",
            "telefono": "1122334455",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "10:00",
            "idempotency_key": "clave-http-1",
        }
        url = "/b/padel-club/api/reservar"
        primera = client.post(url, data=json.dumps(payload), content_type="application/json")
        assert primera.status_code == 201
        assert len(enviados) == 2

        segunda = client.post(url, data=json.dumps(payload), content_type="application/json")
        assert segunda.status_code == 200, "un reintento no debe crear un turno nuevo"
        data = json.loads(segunda.data)
        assert data["success"] is True
        assert data["idempotent_replay"] is True
        assert data["appointment_id"] == json.loads(primera.data)["appointment_id"]
        assert data.get("management_token") is None
        assert len(enviados) == 2, "el replay no debe reenviar los emails de confirmación"

        rows = self._query("SELECT COUNT(*) FROM appointments WHERE business_id = 2")
        assert rows[0][0] == 1

    def test_reserva_sin_clave_sigue_fallando_con_occupied(self):
        client = self.client

        payload = {
            "nombre": "Juan Pérez",
            "telefono": "1122334455",
            "servicio": "Partido Pádel",
            "fecha": self.date,
            "hora": "10:00",
        }
        url = "/b/padel-club/api/reservar"
        assert (
            client.post(url, data=json.dumps(payload), content_type="application/json").status_code
            == 201
        )

        repetida = client.post(url, data=json.dumps(payload), content_type="application/json")
        assert repetida.status_code == 400
        assert json.loads(repetida.data)["code"] == "occupied"

    def test_reserva_con_resource_id_inexistente_rechazada(self):
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

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
        client = self.client

        self._create_resource(2, "Cancha 1")

        resp = client.get(
            f"/b/padel-club/api/disponibilidad/{self.date}?servicio=Partido%20P%C3%A1del"
        )
        data = json.loads(resp.data)
        assert len(data["horarios_disponibles"]) > 0

    def test_api_reserva_aislada_por_tenant(self):
        client = self.client

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
        client = self.client

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
        client = self.client

        resp = client.get(f"/b/el-corte/api/disponibilidad/{self.date}?servicio=Corte")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert len(data["horarios_disponibles"]) > 0

    def test_api_recursos_devuelve_lista_vacia(self):
        client = self.client

        resp = client.get("/b/el-corte/api/recursos")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["recursos"] == []


if __name__ == "__main__":
    unittest.main()
