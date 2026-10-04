import os
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import app as application
import database.database as database
import scripts.send_reminders as reminder_runner
from services import appointments, notifications

ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")


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
        self._set_notifications(True)

        # "Hoy" fijo: un día futuro que no es domingo. El turno se crea para el
        # día siguiente (nunca domingo). Parcheamos _local_today para que el
        # runner calcule "mañana" = fecha_del_turno.
        self.fixed_today = datetime.now(ZONA_HORARIA).date() + timedelta(days=3)
        while self.fixed_today.weekday() == 6:
            self.fixed_today += timedelta(days=1)
        self.appointment_date = self.fixed_today + timedelta(days=1)
        while self.appointment_date.weekday() == 6:
            self.appointment_date += timedelta(days=1)

    def _set_notifications(self, enabled):
        """Toggle del flag de notificaciones preservando el resto de la config.

        Se usa la API en vez de `UPDATE ... SET notifications_enabled = 1/0`
        porque el adaptador traducía esa forma a `SET col IS TRUE` /
        `SET NOT col`, que es SQL inválido en PostgreSQL. Los campos que la API
        escribe de forma incondicional se reenvían desde el estado actual.
        """
        current = database.get_business_settings_scoped(1)
        database.update_business_settings_scoped(
            1,
            current["business_name"],
            current["business_type"],
            current["business_initials"],
            current["business_description"],
            current["timezone"],
            notifications_enabled=enabled,
        )

    def _run_with_smtp(self, fake):
        # `_run_once()` es un runner de CLI: `_init_cli_pool()` resuelve el pool a
        # partir de la variable de entorno DATABASE_URL. El harness deja esa
        # variable apuntando a la base de mantenimiento compartida mientras el pool
        # de la app sí usa la base temporal del test, así que se alinea el entorno
        # con la URL por test para que el runner opere sobre la base aislada.
        env = {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "DATABASE_URL": application.app.config["DATABASE_URL"],
        }
        # El runner calcula "mañana" = _local_today(...). Para que el turno
        # (creado en self.appointment_date) sea candidato, _local_today debe
        # devolver exactamente esa fecha.
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
            mock.patch.object(reminder_runner, "_local_today", return_value=self.appointment_date),
        ):
            return reminder_runner._run_once()


class TestReminderRunner(BaseReminderTest):
    def _confirmed(self, hora, email):
        result = appointments.create_appointment(
            "Ana Pérez",
            "3838439222",
            "Corte",
            self.appointment_date.isoformat(),
            hora,
            1,
            email=email,
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
        self._set_notifications(False)
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
