"""Cobertura de acciones del panel admin previamente sin tests HTTP.

Cubre: cancelación de turnos, reprogramación con recurso, recálculo de
fidelización e inteligencia (preguntas frecuentes / oportunidades). Sigue el
patrón de login del admin (password + CSRF) usado por ``test_admin_panel.py``.
"""

import hashlib
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from services import appointments, loyalty
from services.knowledge import create_knowledge_scoped

BUSINESS_2_SQL = "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"


def _next_open_day():
    """Primer día (desde mañana) con turno disponible a las 15:00 para negocio 1."""
    day = date.today() + timedelta(days=1)
    for _ in range(60):
        if "15:00" in appointments.get_available_slots(day.isoformat(), 1, duration=30):
            return day.isoformat()
        day += timedelta(days=1)
    raise AssertionError("No se encontró un día abierto para negocio 1")


def _make_business_2():
    connection = database.get_connection()
    try:
        connection.execute(BUSINESS_2_SQL)
        connection.commit()
    finally:
        connection.close()


class _AdminLoginBase(unittest.TestCase):
    """Base que loguea como owner del negocio 1 vía /login (password + CSRF)."""

    def setUp(self):
        application.rate_limit_state.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self._original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self._tmp.name) / "probe.db"
        database.init_database()

        self._original_hash = application.ADMIN_PASSWORD_HASH
        self._original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

        self.client = application.app.test_client()
        login_page = self.client.get("/login")
        self.csrf_login = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        self.client.post("/login", data={"password": "correcta", "csrf_token": self.csrf_login})

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self._original_hash
        application.ADMIN_PASSWORD = self._original_password
        database.DATABASE_PATH = self._original_database_path
        self._tmp.cleanup()

    def _admin_csrf(self):
        page = self.client.get("/admin")
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return match.group(1) if match else self.csrf_login

    def _exec(self, sql, params=()):
        connection = database.get_connection()
        try:
            result = connection.execute(sql, params)
            connection.commit()
            return result.fetchall()
        finally:
            connection.close()

    def _query(self, sql, params=()):
        connection = database.get_connection()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def _create_turno(self, time_="09:00", resource_id=None):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", _next_open_day(), time_, 1, resource_id=resource_id
        )
        self.assertTrue(result["success"], result)
        return result["appointment_id"]

    def _appointment_row(self, appointment_id):
        rows = self._query("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
        return rows[0] if rows else None


class TestAdminCancelAppointment(_AdminLoginBase):
    """Cancelación de turnos desde el admin (endpoint con slug)."""

    SLUG_PREFIX = "/b/el-corte/admin"

    def test_cancel_redirects_and_cancels_appointment(self):
        apt = self._create_turno()
        response = self.client.post(
            f"{self.SLUG_PREFIX}/turnos/{apt}/cancelar", data={"csrf_token": self._admin_csrf()}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._appointment_row(apt)["status"], "cancelled")

    def test_cancel_json_returns_json_and_cancels(self):
        apt = self._create_turno()
        response = self.client.post(
            f"{self.SLUG_PREFIX}/turnos/{apt}/cancelar",
            data={"csrf_token": self._admin_csrf()},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(self._appointment_row(apt)["status"], "cancelled")

    def test_cancel_without_csrf_rejected(self):
        apt = self._create_turno()
        response = self.client.post(f"{self.SLUG_PREFIX}/turnos/{apt}/cancelar", data={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._appointment_row(apt)["status"], "confirmed")


class TestAdminRescheduleWithResource(_AdminLoginBase):
    """Reprogramación de turnos indicando recurso objetivo."""

    def test_reschedule_with_resource_assigns_resource(self):
        resource = database.create_resource_scoped(1, "Silla 1")
        apt = self._create_turno()
        new_date = _next_open_day()
        response = self.client.post(
            f"/admin/turnos/{apt}/reprogramar/con-recurso",
            data={
                "csrf_token": self._admin_csrf(),
                "new_date": new_date,
                "new_time": "15:00",
                "resource_id": str(resource),
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        row = self._appointment_row(apt)
        self.assertEqual(row["resource_id"], resource)
        self.assertEqual(row["appointment_date"], new_date)
        self.assertEqual(row["appointment_time"], "15:00")

    def test_reschedule_without_resource_keeps_existing(self):
        resource = database.create_resource_scoped(1, "Silla 1")
        apt = self._create_turno(resource_id=resource)
        new_date = _next_open_day()
        response = self.client.post(
            f"/admin/turnos/{apt}/reprogramar/con-recurso",
            data={"csrf_token": self._admin_csrf(), "new_date": new_date, "new_time": "15:00"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(self._appointment_row(apt)["resource_id"], resource)

    def test_reschedule_with_foreign_resource_rejected(self):
        _make_business_2()
        foreign_resource = database.create_resource_scoped(2, "Silla de Business B")
        apt = self._create_turno()
        new_date = _next_open_day()
        response = self.client.post(
            f"/admin/turnos/{apt}/reprogramar/con-recurso",
            data={
                "csrf_token": self._admin_csrf(),
                "new_date": new_date,
                "new_time": "15:00",
                "resource_id": str(foreign_resource),
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        row = self._appointment_row(apt)
        self.assertEqual(row["appointment_date"], _next_open_day())
        self.assertEqual(row["appointment_time"], "09:00")
        self.assertIsNone(row["resource_id"])


class TestAdminFidelizacionRecalcular(_AdminLoginBase):
    """Recálculo de saldos de fidelización desde el admin."""

    def _complete_turno(self):
        apt = self._create_turno()
        response = self.client.post(
            f"/admin/turnos/{apt}/estado",
            data={"csrf_token": self._admin_csrf(), "status": "completed"},
        )
        self.assertEqual(response.status_code, 302)
        return apt

    def _account_row(self):
        rows = self._query("SELECT * FROM loyalty_accounts WHERE business_id = 1")
        return rows[0] if rows else None

    def test_recalculate_restores_balance_from_ledger(self):
        database.update_loyalty_settings_scoped(1, True, 10)
        self._complete_turno()
        account = self._account_row()
        self.assertIsNotNone(account)
        self.assertEqual(account["points_balance"], 10)

        self._exec(
            "UPDATE loyalty_accounts SET points_balance = 999 WHERE id = ?", (account["id"],)
        )

        response = self.client.post(
            "/admin/fidelizacion/recalcular", data={"csrf_token": self._admin_csrf()}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._account_row()["points_balance"], 10)

    def test_rebalance_service_rebuilds_all_accounts(self):
        database.update_loyalty_settings_scoped(1, True, 10)
        self._complete_turno()
        apt2 = self._create_turno(time_="10:00")
        self.client.post(
            f"/admin/turnos/{apt2}/estado",
            data={"csrf_token": self._admin_csrf(), "status": "completed"},
        )
        account_ids = [
            r["id"] for r in self._query("SELECT id FROM loyalty_accounts WHERE business_id = 1")
        ]
        for account_id in account_ids:
            self._exec(
                "UPDATE loyalty_accounts SET points_balance = 42 WHERE id = ?", (account_id,)
            )

        result = loyalty.rebalance(1)
        self.assertTrue(result)
        for account_id in account_ids:
            expected = self._query(
                "SELECT COALESCE(SUM(delta), 0) AS total FROM points_ledger WHERE account_id = ?",
                (account_id,),
            )[0]["total"]
            self.assertNotEqual(
                self._query(
                    "SELECT points_balance FROM loyalty_accounts WHERE id = ?", (account_id,)
                )[0]["points_balance"],
                42,
            )
            self.assertEqual(
                self._query(
                    "SELECT points_balance FROM loyalty_accounts WHERE id = ?", (account_id,)
                )[0]["points_balance"],
                expected,
            )


class TestAdminInteligenciaPages(_AdminLoginBase):
    """Páginas de inteligencia: preguntas frecuentes y oportunidades."""

    def _seed_analytics(self, question, count=5, needs_human=2):
        self._exec(
            """
            INSERT INTO conversation_analytics
                (business_id, question_hash, question_text, count, needs_human_count)
            VALUES (1, ?, ?, ?, ?)
            """,
            (hashlib.sha1(question.encode()).hexdigest(), question, count, needs_human),
        )

    def test_inteligencia_shows_frequent_questions(self):
        self._seed_analytics("¿Cuánto cuesta el corte?")
        response = self.client.get("/admin/inteligencia")
        self.assertEqual(response.status_code, 200)
        self.assertIn("¿Cuánto cuesta el corte?", response.text)
        self.assertIn("needs-human-badge", response.text)

    def test_oportunidades_shows_question_without_knowledge(self):
        self._seed_analytics("¿Hacen cortes a domicilio?")
        response = self.client.get("/admin/inteligencia/oportunidades")
        self.assertEqual(response.status_code, 200)
        self.assertIn("¿Hacen cortes a domicilio?", response.text)

    def test_oportunidad_hidden_when_knowledge_exists(self):
        self._seed_analytics("¿Cuánto cuesta el corte?")
        create_knowledge_scoped(1, "faq", "¿Cuánto cuesta el corte?", "1000", "", None)
        response = self.client.get("/admin/inteligencia/oportunidades")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("¿Cuánto cuesta el corte?", response.text)


if __name__ == "__main__":
    unittest.main()
