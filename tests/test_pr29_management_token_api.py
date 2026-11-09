import hashlib
import secrets
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import database.database as database
from routes import public_api
from services import appointments
from tests._pg_compat import PostgreSQLTestCase

pytestmark = pytest.mark.no_pg


def next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class TestPR29ManagementTokenApi(PostgreSQLTestCase):
    def _execute(self, app, sql, params=None):
        with app.app_context():
            connection = database.get_connection()
            try:
                connection.execute(sql, params or ())
                connection.commit()
            finally:
                connection.close()

    def _query(self, app, sql, params=None):
        with app.app_context():
            connection = database.get_connection()
            try:
                return connection.execute(sql, params or ()).fetchall()
            finally:
                connection.close()

    def _seed_second_business(self, app):
        self._execute(
            app,
            "INSERT INTO businesses (id, name, slug, active, pending) VALUES (%s, %s, %s, TRUE, FALSE)",
            (2, "Business B", "business-b"),
        )
        self._execute(
            app,
            "INSERT INTO services (id, business_id, name, price, duration, active) VALUES (4, %s, %s, %s, %s, TRUE)",
            (2, "Corte B", 12000, 30),
        )
        for day in range(7):
            self._execute(
                app,
                "INSERT INTO weekly_schedules (business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
                "VALUES (2, %s, TRUE, '09:00', '13:00', '15:00', '19:00')",
                (day,),
            )

    @staticmethod
    def _business(business_id):
        return {
            1: {"id": 1, "name": "El Corte", "slug": "el-corte"},
            2: {"id": 2, "name": "Business B", "slug": "business-b"},
        }[business_id]

    def _create_appointment(self, app, name, phone, service, hour, business_id):
        with app.app_context():
            result = appointments.create_appointment(
                name, phone, service, next_open_day(), hour, business_id=business_id
            )
        assert result["success"], result
        return result

    @staticmethod
    def _foreign_token():
        return secrets.token_urlsafe(32)

    def test_turnos_post_json_with_header_token_returns_only_scoped_turnos(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)
        self._create_appointment(app, "Cliente B", "3838439333", "Corte B", "10:00", 2)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/turnos",
                json={"nombre": "Cliente A", "telefono": "3838439222"},
                headers={"X-Management-Token": created["management_token"]},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert len(data["turnos"]) == 1
        assert data["turnos"][0]["customer_name"] == "Cliente A"
        assert data["turnos"][0]["business_id"] == 1
        assert "management_token_hash" not in data["turnos"][0]
        assert "management_token" not in data["turnos"][0]

    def test_turnos_post_json_accepts_body_token_for_compatibility(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/turnos",
                json={
                    "nombre": "Cliente A",
                    "telefono": "3838439222",
                    "management_token": created["management_token"],
                },
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        assert len(response.get_json()["turnos"]) == 1

    def test_turnos_missing_token_returns_verification_required_without_personal_data(self, app, client):
        self._seed_second_business(app)
        self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/turnos",
                json={"nombre": "Cliente A", "telefono": "3838439222"},
            )

        assert response.status_code == 401
        data = response.get_json()
        assert data["code"] == "verification_required"
        body = response.get_data(as_text=True)
        assert "3838439222" not in body
        assert "Cliente A" not in body
        assert "management_token_hash" not in body

    def test_turnos_invalid_token_returns_verification_required(self, app, client):
        self._seed_second_business(app)
        self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/turnos",
                json={"nombre": "Cliente A", "telefono": "3838439222"},
                headers={"X-Management-Token": self._foreign_token()},
            )

        assert response.status_code == 401
        assert response.get_json()["code"] == "verification_required"

    def test_turnos_token_from_other_business_is_rejected_without_cross_tenant_leak(self, app, client):
        self._seed_second_business(app)
        created_b = self._create_appointment(app, "Cliente B", "3838439333", "Corte B", "10:00", 2)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/turnos",
                json={"nombre": "Cliente B", "telefono": "3838439333"},
                headers={"X-Management-Token": created_b["management_token"]},
            )

        assert response.status_code == 401
        assert response.get_json()["code"] == "verification_required"
        body = response.get_data(as_text=True)
        assert "Cliente B" not in body
        assert "3838439333" not in body
        assert "Corte B" not in body

    def test_cancel_with_valid_token_scoped_to_business(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/cancelar",
                json={
                    "appointment_id": created["appointment_id"],
                    "telefono": "3838439222",
                    "customer_name": "Cliente A",
                    "management_token": created["management_token"],
                },
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        status = self._query(app, "SELECT status FROM appointments WHERE id = %s", (created["appointment_id"],))[0]["status"]
        assert status == "cancelled"

    def test_cancel_with_invalid_token_does_not_cancel(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/cancelar",
                json={
                    "appointment_id": created["appointment_id"],
                    "telefono": "3838439222",
                    "customer_name": "Cliente A",
                    "management_token": self._foreign_token(),
                },
            )

        assert response.status_code == 400
        status = self._query(app, "SELECT status FROM appointments WHERE id = %s", (created["appointment_id"],))[0]["status"]
        assert status == "confirmed"

    def test_cancel_with_token_from_other_business_does_not_cross_tenant(self, app, client):
        self._seed_second_business(app)
        created_b = self._create_appointment(app, "Cliente B", "3838439333", "Corte B", "10:00", 2)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/cancelar",
                json={
                    "appointment_id": created_b["appointment_id"],
                    "telefono": "3838439333",
                    "customer_name": "Cliente B",
                    "management_token": created_b["management_token"],
                },
            )

        assert response.status_code == 400
        status = self._query(app, "SELECT status FROM appointments WHERE id = %s", (created_b["appointment_id"],))[0]["status"]
        assert status == "confirmed"

    def test_reschedule_with_valid_token_scoped_to_business(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/reprogramar",
                json={
                    "appointment_id": created["appointment_id"],
                    "telefono": "3838439222",
                    "customer_name": "Cliente A",
                    "nueva_fecha": next_open_day(),
                    "nueva_hora": "11:00",
                    "management_token": created["management_token"],
                },
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        row = self._query(app, "SELECT appointment_time, status FROM appointments WHERE id = %s", (created["appointment_id"],))[0]
        assert str(row["appointment_time"]).startswith("11:00")
        assert row["status"] == "confirmed"

    def test_reschedule_with_invalid_token_does_not_move_appointment(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/reprogramar",
                json={
                    "appointment_id": created["appointment_id"],
                    "telefono": "3838439222",
                    "customer_name": "Cliente A",
                    "nueva_fecha": next_open_day(),
                    "nueva_hora": "11:00",
                    "management_token": self._foreign_token(),
                },
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is False
        row = self._query(app, "SELECT appointment_time FROM appointments WHERE id = %s", (created["appointment_id"],))[0]
        assert str(row["appointment_time"]).startswith("09:00")

    def test_reschedule_with_token_from_other_business_does_not_cross_tenant(self, app, client):
        self._seed_second_business(app)
        created_b = self._create_appointment(app, "Cliente B", "3838439333", "Corte B", "10:00", 2)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.post(
                "/b/el-corte/api/reprogramar",
                json={
                    "appointment_id": created_b["appointment_id"],
                    "telefono": "3838439333",
                    "customer_name": "Cliente B",
                    "nueva_fecha": next_open_day(),
                    "nueva_hora": "11:00",
                    "management_token": created_b["management_token"],
                },
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is False
        row = self._query(app, "SELECT appointment_time FROM appointments WHERE id = %s", (created_b["appointment_id"],))[0]
        assert str(row["appointment_time"]).startswith("10:00")

    def test_public_manage_turno_get_valid_token_renders_scoped_appointment(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.get(
                f"/b/el-corte/turno/{created['management_token']}?id={created['appointment_id']}"
            )

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Cliente A" in body
        assert "Corte" in body
        assert "El Corte" in body
        assert 'name="management_token"' not in body
        assert 'type="hidden" name="action" value="cancelar"' in body

    def test_public_manage_turno_invalid_token_returns_not_found_without_leak(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(1)):
            response = client.get(
                f"/b/el-corte/turno/{self._foreign_token()}?id={created['appointment_id']}"
            )

        assert response.status_code == 404
        body = response.get_data(as_text=True)
        assert "Cliente A" not in body
        assert "3838439222" not in body

    def test_public_manage_turno_valid_token_other_business_slug_is_not_resolved(self, app, client):
        self._seed_second_business(app)
        created = self._create_appointment(app, "Cliente A", "3838439222", "Corte", "09:00", 1)

        with patch("app.resolve_business", return_value=self._business(2)):
            response = client.get(
                f"/b/business-b/turno/{created['management_token']}?id={created['appointment_id']}"
            )

        assert response.status_code == 404
        assert "Cliente A" not in response.get_data(as_text=True)
