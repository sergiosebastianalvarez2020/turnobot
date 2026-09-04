import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import app as application
import database.database as database
from services import appointments
from services import notifications
from services.ai import execute_tool


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class ConditionalEmailBase(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.client = application.app.test_client()
        self.date = _next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def set_notifications(self, business_id, enabled):
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET notifications_enabled = ? WHERE business_id = ?",
            (1 if enabled else 0, business_id),
        )
        connection.commit()
        connection.close()

    def reservar_payload(self, email=None, time="09:00"):
        payload = {
            "nombre": "Ana Pérez",
            "telefono": "3838439222",
            "servicio": "Corte",
            "fecha": self.date,
            "hora": time,
        }
        if email is not None:
            payload["email"] = email
        return payload


class TestAPIConditionalEmail(ConditionalEmailBase):
    def test_reserva_sin_email_con_notificaciones_deshabilitadas(self):
        self.set_notifications(1, False)
        response = self.client.post(
            "/api/reservar",
            json=self.reservar_payload(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["success"])

    def test_reserva_sin_email_con_notificaciones_habilitadas_rechazada(self):
        self.set_notifications(1, True)
        response = self.client.post(
            "/api/reservar",
            json=self.reservar_payload(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body["reason"], "email_required")
        self.assertFalse(body["success"])

    def test_reserva_con_email_con_notificaciones_habilitadas_ok(self):
        self.set_notifications(1, True)
        response = self.client.post(
            "/api/reservar",
            json=self.reservar_payload(email="ana@example.com"),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["success"])


class TestAIConditionalEmail(ConditionalEmailBase):
    def _args(self, email=None):
        args = {
            "nombre": "Ana Pérez",
            "telefono": "3838439222",
            "servicio": "Corte",
            "fecha": self.date,
            "hora": "09:00",
        }
        if email is not None:
            args["email"] = email
        return args

    def test_ai_sin_email_con_notificaciones_deshabilitadas_ok(self):
        self.set_notifications(1, False)
        result = execute_tool("reservar_turno", self._args(), 1)
        self.assertTrue(result["success"])

    def test_ai_sin_email_con_notificaciones_habilitadas_pide_email(self):
        self.set_notifications(1, True)
        result = execute_tool("reservar_turno", self._args(), 1)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "email_required")

    def test_ai_con_email_con_notificaciones_habilitadas_ok(self):
        self.set_notifications(1, True)
        result = execute_tool("reservar_turno", self._args(email="ana@example.com"), 1)
        self.assertTrue(result["success"])


class NotificationLogIsolationBase(ConditionalEmailBase):
    def setUp(self):
        super().setUp()
        self.setup_business_2()
        self.set_notifications(1, True)
        self.set_notifications(2, True)

    def setup_business_2(self):
        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
            )
            connection.execute(
                "INSERT INTO business_settings "
                "(business_name, business_type, business_initials, business_description, timezone, slot_duration, break_between_slots, notifications_enabled, business_id) "
                "VALUES ('Business B', 'Barberia', 'BB', 'desc', 'America/Argentina/Buenos_Aires', 30, 0, 1, 2)"
            )
            connection.execute(
                "INSERT INTO services (business_id, name, price, duration, active) "
                "VALUES (2, 'Corte B', 15000, 30, 1)"
            )
            for day in range(6):
                connection.execute(
                    "INSERT INTO weekly_schedules "
                    "(business_id, day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) "
                    "VALUES (2, ?, 1, '09:00', '13:00', '15:00', '19:00')",
                    (day,),
                )
            connection.commit()
        finally:
            connection.close()


class TestNotificationLogIsolation(NotificationLogIsolationBase):
    def _send_confirmation(self, business_id, apt, token, slug):
        """Envía confirmación con SMTP mockeado para que se registre el log."""
        class FakeSMTP:
            def __enter__(self):
                return self
            def __exit__(self, *args, **kwargs):
                return False
            def ehlo(self):
                return (250, b"ok")
            def starttls(self):
                return (220, b"ok")
            def login(self, user, password):
                return
            def sendmail(self, from_addr, to_addrs, msg):
                return

        env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=FakeSMTP()):
            return notifications.send_confirmation_email(
                business_id, apt, management_token=token, slug=slug,
                public_base_url="https://example.com", force=False,
            )

    def test_dos_negocios_no_interfieren(self):
        a = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.date, "09:00", 1,
            email="ana@example.com",
        )
        self.assertTrue(a["success"])

        b = appointments.create_appointment(
            "Bruno López", "3838439555", "Corte B", self.date, "09:00", 2,
            email="bruno@example.com",
        )
        self.assertTrue(b["success"])

        apt_a = {
            "id": a["appointment_id"], "customer_name": "Ana", "customer_email": "ana@example.com",
            "service": "Corte", "appointment_date": self.date,
            "appointment_time": "09:00", "appointment_end": "09:30",
        }
        apt_b = {
            "id": b["appointment_id"], "customer_name": "Bruno", "customer_email": "bruno@example.com",
            "service": "Corte B", "appointment_date": self.date,
            "appointment_time": "09:00", "appointment_end": "09:30",
        }

        sent_a, reason_a = self._send_confirmation(1, apt_a, "tok_a", "business-a")
        sent_b, reason_b = self._send_confirmation(2, apt_b, "tok_b", "business-b")
        self.assertTrue(sent_a, reason_a)
        self.assertTrue(sent_b, reason_b)

        # Idempotencia estricta por tenant: el log del negocio A no afecta al B
        # (mismo appointment_id/type/channel en negocios distintos es independiente).
        self.assertTrue(
            notifications.notification_sent_scoped(1, a["appointment_id"], "confirmation", "email")
        )
        self.assertTrue(
            notifications.notification_sent_scoped(2, b["appointment_id"], "confirmation", "email")
        )
        # El turno del negocio A no se considera enviado dentro del negocio B.
        self.assertFalse(
            notifications.notification_sent_scoped(2, a["appointment_id"], "confirmation", "email")
        )

        # Reintentar en un negocio no impide al otro: enviar de nuevo en el B
        # con el appointment_id del A (hipotético) debe poder registrarse sin
        # colisión del índice único por business.
        connection = database.get_connection()
        ok = connection.execute(
            "SELECT COUNT(*) AS c FROM notification_log WHERE business_id = ? AND appointment_id = ?",
            (2, a["appointment_id"]),
        ).fetchone()["c"]
        connection.close()
        self.assertEqual(ok, 0)


if __name__ == "__main__":
    unittest.main()
