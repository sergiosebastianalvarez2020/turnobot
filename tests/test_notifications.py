import os
import ssl
import unittest
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from unittest import mock

import database.database as database
import scripts.retry_failed_notifications as retry_runner
from services import appointments, notifications
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


class StartTLSFailureSMTP(FakeSMTP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.login_calls = []

    def starttls(self):
        raise notifications.smtplib.SMTPNotSupportedError("STARTTLS unavailable")

    def login(self, user, password):
        self.login_calls.append((user, password))


class SSLErrorSMTP(FakeSMTP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.login_calls = []

    def starttls(self):
        raise ssl.SSLError("certificate verify failed")

    def login(self, user, password):
        self.login_calls.append((user, password))


class LoginFailureSMTP(FakeSMTP):
    def login(self, user, password):
        raise notifications.smtplib.SMTPAuthenticationError(535, "Authentication failed")


class BaseNotificationTest(unittest.TestCase):
    def setUp(self):
        self.valid_date = self._next_open_day()

    @staticmethod
    def _next_open_day():
        date = datetime.now().date() + timedelta(days=1)
        while date.weekday() == 6:
            date += timedelta(days=1)
        return date.isoformat()


class TestEmailCapture(BaseNotificationTest):
    def test_create_appointment_persiste_email(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="ana@example.com"
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["customer_email"], "ana@example.com")

        connection = database.get_connection()
        row = connection.execute(
            "SELECT customer_email FROM appointments WHERE id = ?", (result["appointment_id"],)
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
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="no-es-un-email"
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_email")


class TestConfirmationEmail(BaseNotificationTest):
    def _appointment(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="ana@example.com"
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
        env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587", "EMAIL_FROM": "no-reply@test.com"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            sent1, reason1 = send_confirmation_email(
                1,
                apt,
                management_token="tokensecreto",
                slug="elcorte",
                public_base_url="https://example.com",
                force=True,
            )
            sent2, reason2 = send_confirmation_email(
                1,
                apt,
                management_token="tokensecreto",
                slug="elcorte",
                public_base_url="https://example.com",
                force=True,
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
        self.assertIn(
            "/b/elcorte/turno/tokensecreto?id={}".format(result["appointment_id"]), msg_text
        )

    def test_respeto_notifications_enabled(self):
        result, apt = self._appointment()
        fake = FakeSMTP()
        env = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587"}
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            sent, reason = send_confirmation_email(1, apt, force=False)

        self.assertFalse(sent)
        self.assertEqual(reason, "disabled")
        self.assertEqual(len(fake.messages), 0)

    def test_starttls_fallido_no_autentica_ni_envia(self):
        _, apt = self._appointment()
        fake = StartTLSFailureSMTP()
        env = {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "SMTP_USER": "user",
            "SMTP_PASSWORD": "secret-test-password",
            "EMAIL_FROM": "no-reply@test.com",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            sent, reason = send_confirmation_email(1, apt, force=True)

        self.assertFalse(sent)
        self.assertEqual(reason, "send_failed")
        self.assertEqual(fake.login_calls, [])
        self.assertEqual(fake.messages, [])
        state = database.get_notification_state_scoped(
            1, apt["id"], notifications.CONFIRMATION, "email"
        )
        self.assertNotIn("secret-test-password", state["error"])
        self.assertTrue(state["error"].startswith("starttls_failed:"))
        self.assertIn("STARTTLS unavailable", state["error"])

    def test_starttls_ssl_error_identifica_fallo_y_sanitiza(self):
        _, apt = self._appointment()
        fake = SSLErrorSMTP()
        env = {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "SMTP_USER": "user",
            "SMTP_PASSWORD": "secret-test-password",
            "EMAIL_FROM": "no-reply@test.com",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            sent, reason = send_confirmation_email(1, apt, force=True)

        self.assertFalse(sent)
        self.assertEqual(reason, "send_failed")
        self.assertEqual(fake.login_calls, [])
        self.assertEqual(fake.messages, [])
        state = database.get_notification_state_scoped(
            1, apt["id"], notifications.CONFIRMATION, "email"
        )
        self.assertTrue(state["error"].startswith("starttls_failed:"))
        self.assertIn("certificate verify failed", state["error"])
        self.assertNotIn("secret-test-password", state["error"])

    def test_error_smtp_general_no_lleva_prefijo_starttls(self):
        _, apt = self._appointment()
        fake = LoginFailureSMTP()
        env = {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "SMTP_USER": "user",
            "SMTP_PASSWORD": "secret-test-password",
            "EMAIL_FROM": "no-reply@test.com",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            sent, reason = send_confirmation_email(1, apt, force=True)

        self.assertFalse(sent)
        self.assertEqual(reason, "send_failed")
        state = database.get_notification_state_scoped(
            1, apt["id"], notifications.CONFIRMATION, "email"
        )
        self.assertFalse(state["error"].startswith("starttls_failed:"))
        self.assertIn("Authentication failed", state["error"])


class TestAppointmentByToken(BaseNotificationTest):
    def test_devuelve_turno_con_token_correcto(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="ana@example.com"
        )
        apt = appointments.get_appointment_by_token(
            result["appointment_id"], 1, result["management_token"]
        )
        self.assertIsNotNone(apt)
        self.assertEqual(apt["customer_name"], "Ana Pérez")

    def test_token_incorrecto_devuelve_none(self):
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "10:00", 1, email="ana@example.com"
        )
        apt = appointments.get_appointment_by_token(result["appointment_id"], 1, "token-invalido")
        self.assertIsNone(apt)


class TestRetryFailedNotificationsPacing(unittest.TestCase):
    def test_pausa_entre_elementos_del_lote(self):
        fake_failed = [
            {
                "id": 1,
                "business_id": 1,
                "appointment_id": 101,
                "type": "confirmation",
                "destination": "a@test.com",
            },
            {
                "id": 2,
                "business_id": 1,
                "appointment_id": 102,
                "type": "confirmation",
                "destination": "b@test.com",
            },
            {
                "id": 3,
                "business_id": 1,
                "appointment_id": 103,
                "type": "confirmation",
                "destination": "c@test.com",
            },
        ]
        settings = {"notifications_enabled": 1}
        with (
            mock.patch.object(retry_runner, "smtp_configured", return_value=True),
            mock.patch.object(retry_runner, "_init_cli_pool", return_value=None),
            mock.patch.object(retry_runner, "close_pg_pool"),
            mock.patch.object(
                retry_runner, "list_failed_notifications_scoped", return_value=fake_failed
            ),
            mock.patch.object(retry_runner, "get_business_settings_scoped", return_value=settings),
            mock.patch.object(retry_runner, "_resend", return_value=(True, None)) as mock_resend,
            mock.patch("time.sleep") as mock_sleep,
        ):
            ok_count = retry_runner._run_once()

        self.assertEqual(ok_count, 3)
        self.assertEqual(mock_resend.call_count, 3)
        # Pausa entre elementos: 2 pausas para 3 elementos (no después del último)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_has_calls([mock.call(0.5), mock.call(0.5)])

    def test_un_solo_elemento_no_genera_pausa(self):
        fake_failed = [
            {
                "id": 1,
                "business_id": 1,
                "appointment_id": 101,
                "type": "confirmation",
                "destination": "a@test.com",
            }
        ]
        settings = {"notifications_enabled": 1}
        with (
            mock.patch.object(retry_runner, "smtp_configured", return_value=True),
            mock.patch.object(retry_runner, "_init_cli_pool", return_value=None),
            mock.patch.object(retry_runner, "close_pg_pool"),
            mock.patch.object(
                retry_runner, "list_failed_notifications_scoped", return_value=fake_failed
            ),
            mock.patch.object(retry_runner, "get_business_settings_scoped", return_value=settings),
            mock.patch.object(retry_runner, "_resend", return_value=(True, None)),
            mock.patch("time.sleep") as mock_sleep,
        ):
            ok_count = retry_runner._run_once()

        self.assertEqual(ok_count, 1)
        # 1 elemento -> no debe haber pausa después del único elemento
        mock_sleep.assert_not_called()

    def test_lote_vacio_no_genera_pausas(self):
        with (
            mock.patch.object(retry_runner, "smtp_configured", return_value=True),
            mock.patch.object(retry_runner, "_init_cli_pool", return_value=None),
            mock.patch.object(retry_runner, "close_pg_pool"),
            mock.patch.object(retry_runner, "list_failed_notifications_scoped", return_value=[]),
            mock.patch("time.sleep") as mock_sleep,
        ):
            ok_count = retry_runner._run_once()

        self.assertEqual(ok_count, 0)
        mock_sleep.assert_not_called()

    def test_fallo_individual_continua_y_mantiene_pausa(self):
        fake_failed = [
            {
                "id": 1,
                "business_id": 1,
                "appointment_id": 101,
                "type": "confirmation",
                "destination": "a@test.com",
            },
            {
                "id": 2,
                "business_id": 1,
                "appointment_id": 102,
                "type": "confirmation",
                "destination": "b@test.com",
            },
        ]
        settings = {"notifications_enabled": 1}
        with (
            mock.patch.object(retry_runner, "smtp_configured", return_value=True),
            mock.patch.object(retry_runner, "_init_cli_pool", return_value=None),
            mock.patch.object(retry_runner, "close_pg_pool"),
            mock.patch.object(
                retry_runner, "list_failed_notifications_scoped", return_value=fake_failed
            ),
            mock.patch.object(retry_runner, "get_business_settings_scoped", return_value=settings),
            mock.patch.object(
                retry_runner, "_resend", side_effect=[(False, "send_failed"), (True, None)]
            ) as mock_resend,
            mock.patch("time.sleep") as mock_sleep,
        ):
            ok_count = retry_runner._run_once()

        self.assertEqual(ok_count, 1)
        self.assertEqual(mock_resend.call_count, 2)
        # Falló el primero, pero continúa con el segundo y respeta la pausa entre ambos
        mock_sleep.assert_called_once_with(0.5)

    def test_negocio_deshabilitado_se_omite(self):
        fake_failed = [
            {
                "id": 1,
                "business_id": 1,
                "appointment_id": 101,
                "type": "confirmation",
                "destination": "a@test.com",
            }
        ]
        settings = {"notifications_enabled": 0}
        with (
            mock.patch.object(retry_runner, "smtp_configured", return_value=True),
            mock.patch.object(retry_runner, "_init_cli_pool", return_value=None),
            mock.patch.object(retry_runner, "close_pg_pool"),
            mock.patch.object(
                retry_runner, "list_failed_notifications_scoped", return_value=fake_failed
            ),
            mock.patch.object(retry_runner, "get_business_settings_scoped", return_value=settings),
            mock.patch.object(retry_runner, "_resend") as mock_resend,
            mock.patch("time.sleep") as mock_sleep,
        ):
            ok_count = retry_runner._run_once()

        self.assertEqual(ok_count, 0)
        mock_resend.assert_not_called()
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
