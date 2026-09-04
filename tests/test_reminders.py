import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import database.database as database
from services import appointments
from services import notifications
import scripts.send_reminders as reminder_runner


class FakeSMTP:
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


class BaseReminderTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.enable_notifications()

        # "Hoy" fijo: un día futuro que no es domingo. El turno se crea para el
        # día siguiente (nunca domingo). Parcheamos _local_today para que el
        # runner calcule "mañana" = fecha_del_turno.
        self.fixed_today = datetime.now().date() + timedelta(days=3)
        while self.fixed_today.weekday() == 6:
            self.fixed_today += timedelta(days=1)
        self.appointment_date = self.fixed_today + timedelta(days=1)

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def enable_notifications(self):
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET notifications_enabled = 1 WHERE business_id = 1"
        )
        connection.commit()
        connection.close()

    def _run_with_smtp(self, fake):
        env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587"}
        # El runner calcula "mañana" = _local_today(...). Para que el turno
        # (creado en self.appointment_date) sea candidato, _local_today debe
        # devolver exactamente esa fecha.
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake), \
             mock.patch.object(reminder_runner, "_local_today", return_value=self.appointment_date):
            return reminder_runner._run_once()


class TestReminderRunner(BaseReminderTest):
    def _confirmed(self, hora, email):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte",
            self.appointment_date.isoformat(), hora, 1, email=email,
        )
        self.assertTrue(result["success"])
        return result

    def test_envia_recordatorio_24h_y_es_idempotente(self):
        self._confirmed("09:00", "ana@example.com")

        fake = FakeSMTP()
        sent_first = self._run_with_smtp(fake)
        sent_second = self._run_with_smtp(fake)

        self.assertEqual(sent_first, 1)
        self.assertEqual(sent_second, 0)
        self.assertEqual(len(fake.messages), 1)
        self.assertEqual(fake.messages[0][1], ["ana@example.com"])
        self.assertIn("Recordatorio", str(fake.messages[0][2]))

    def test_no_envia_con_notificaciones_deshabilitadas(self):
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET notifications_enabled = 0 WHERE business_id = 1"
        )
        connection.commit()
        connection.close()
        self._confirmed("09:00", "ana@example.com")

        fake = FakeSMTP()
        sent = self._run_with_smtp(fake)

        self.assertEqual(sent, 0)
        self.assertEqual(len(fake.messages), 0)

    def test_no_envia_sin_email(self):
        self._confirmed("09:00", None)

        fake = FakeSMTP()
        sent = self._run_with_smtp(fake)

        self.assertEqual(sent, 0)
        self.assertEqual(len(fake.messages), 0)


if __name__ == "__main__":
    unittest.main()
