import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from email import policy
from email.parser import BytesParser

import database.database as database
from services import appointments
from services import notifications
from services.notifications import send_confirmation_email


def _decode_mime_text(msg):
    """Devuelve el contenido del primer part text/plain de un mensaje MIME,
    decodificando correctamente el base64 (con salto de línea)."""
    parsed = BytesParser(policy=policy.default).parsebytes(str(msg).encode("utf-8"))
    for part in parsed.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content() or ""
    return str(msg)


class FakeSMTP:
    """Fake cliente SMTP para pruebas sin red."""

    def __init__(self, *args, **kwargs):
        self.messages = []

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
        self.messages.append((from_addr, to_addrs, msg))


class BaseNotificationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = self._next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()


class TestEmailCapture(BaseNotificationTest):
    def test_create_appointment_persiste_email(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte",
            self.valid_date, "09:00", 1, email="ana@example.com",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["customer_email"], "ana@example.com")

        connection = database.get_connection()
        row = connection.execute(
            "SELECT customer_email FROM appointments WHERE id = ?",
            (result["appointment_id"],),
        ).fetchone()
        connection.close()
        self.assertEqual(row["customer_email"], "ana@example.com")

    def test_create_appointment_email_opcional(self):
        result = appointments.create_appointment(
            "Juan López", "3838439333", "Corte", self.valid_date, "10:00", 1
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["customer_email"])

    def test_create_appointment_email_invalido(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte",
            self.valid_date, "09:00", 1, email="no-es-un-email",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_email")


class TestConfirmationEmail(BaseNotificationTest):
    def _appointment(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte",
            self.valid_date, "09:00", 1, email="ana@example.com",
        )
        return result, {
            "id": result["appointment_id"],
            "customer_name": "Ana Pérez",
            "customer_email": "ana@example.com",
            "service": "Corte",
            "appointment_date": self.valid_date,
            "appointment_time": "09:00",
            "appointment_end": "09:30",
        }

    def test_semis_sin_smtp_no_falla_ni_registra(self):
        result, apt = self._appointment()
        with mock.patch.dict(os.environ, {}, clear=False):
            if "SMTP_HOST" in os.environ:
                del os.environ["SMTP_HOST"]
            sent, reason = send_confirmation_email(
                1, apt, management_token="token", slug="elcorte", force=True
            )
        self.assertFalse(sent)
        self.assertEqual(reason, "smtp_not_configured")

        connection = database.get_connection()
        count = connection.execute(
            "SELECT COUNT(*) AS c FROM notification_log WHERE appointment_id = ?",
            (result["appointment_id"],),
        ).fetchone()["c"]
        connection.close()
        self.assertEqual(count, 0)

    def test_envia_y_es_idempotente(self):
        result, apt = self._appointment()
        fake = FakeSMTP()
        env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587",
               "EMAIL_FROM": "no-reply@test.com"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            sent1, reason1 = send_confirmation_email(
                1, apt, management_token="tokensecreto", slug="elcorte",
                public_base_url="https://example.com", force=True,
            )
            sent2, reason2 = send_confirmation_email(
                1, apt, management_token="tokensecreto", slug="elcorte",
                public_base_url="https://example.com", force=True,
            )

        self.assertTrue(sent1)
        self.assertIsNone(reason1)
        # Segundo envío del mismo tipo/canal debe omitirse (idempotente).
        self.assertFalse(sent2)
        self.assertEqual(reason2, "already_sent")
        self.assertEqual(len(fake.messages), 1)

        # El enlace de gestión debe incluir el token y el id del turno.
        to_addr = fake.messages[0][1]
        msg_text = _decode_mime_text(fake.messages[0][2])
        self.assertEqual(to_addr, ["ana@example.com"])
        self.assertIn("/b/elcorte/turno/tokensecreto?id={}".format(result["appointment_id"]), msg_text)

    def test_respeto_notifications_enabled(self):
        result, apt = self._appointment()
        fake = FakeSMTP()
        env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            sent, reason = send_confirmation_email(1, apt, force=False)

        self.assertFalse(sent)
        self.assertEqual(reason, "disabled")
        self.assertEqual(len(fake.messages), 0)


class TestAppointmentByToken(BaseNotificationTest):
    def test_devuelve_turno_con_token_correcto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte",
            self.valid_date, "09:00", 1, email="ana@example.com",
        )
        apt = appointments.get_appointment_by_token(
            result["appointment_id"], 1, result["management_token"]
        )
        self.assertIsNotNone(apt)
        self.assertEqual(apt["customer_name"], "Ana Pérez")

    def test_token_incorrecto_devuelve_none(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte",
            self.valid_date, "10:00", 1, email="ana@example.com",
        )
        apt = appointments.get_appointment_by_token(
            result["appointment_id"], 1, "token-invalido"
        )
        self.assertIsNone(apt)


if __name__ == "__main__":
    unittest.main()
