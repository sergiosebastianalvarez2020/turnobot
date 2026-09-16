"""Bloque J - Hardening de notificaciones y efectos secundarios.

Garantías verificadas sobre la relación operación de turno <-> notificación:

- Cancelación (éxito, rechazo, no autorizada): NUNCA genera notificaciones ni
  registros falsos de éxito en `notification_log`.
- Reprogramación: ídem; los reintentos tampoco generan efectos indebidos.
- Creación por API: la reserva exitosa genera confirmación + aviso al negocio;
  una reserva rechazada/ocupada/duplicada NO genera confirmaciones; una doble
  reserva genera UNA sola confirmación.
- Reintentos: una fila `failed` solo se re-despacha si el turno sigue
  existiendo y está `confirmed` en su negocio (consistencia `notification_log`
  <-> `appointments`). Nunca se reenvía para turnos cancelados, inexistentes o
  de otro negocio; dos retries simultáneos no duplican el envío.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import app as application
import database.database as database
from database.database import list_failed_notifications_scoped
from services import appointments, notifications
from services.notifications import (
    BUSINESS_CONFIRMATION,
    CONFIRMATION,
    send_confirmation_email,
)
import scripts.retry_failed_notifications as retry_runner

SMTP_ENV = {
    "SMTP_HOST": "smtp.test",
    "SMTP_PORT": "587",
    "EMAIL_FROM": "no-reply@test.com",
}


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


class FailingSMTP(FakeSMTP):
    def sendmail(self, from_addr, to_addrs, msg):
        raise ConnectionRefusedError("smtp down")


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class NotificationsHardeningBase(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = _next_open_day()
        self.client = application.app.test_client()
        self._enable_notifications()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _enable_notifications(self, notification_email=""):
        c = database.get_connection()
        c.execute(
            """UPDATE business_settings
               SET notifications_enabled = 1, notification_email = ?
               WHERE business_id = 1""",
            (notification_email,),
        )
        c.commit()
        c.close()

    @staticmethod
    def _query(sql, params=None):
        c = database.get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    def _log_count(self, appointment_id=None):
        sql = "SELECT COUNT(*) AS n FROM notification_log"
        params = None
        if appointment_id is not None:
            sql += " WHERE appointment_id = ?"
            params = (appointment_id,)
        return self._query(sql, params)[0]["n"]

    def _notification_row(self, appointment_id, type_):
        rows = self._query(
            """SELECT status, error, destination FROM notification_log
               WHERE appointment_id = ? AND type = ?""",
            (appointment_id, type_),
        )
        return dict(rows[0]) if rows else None

    def _appointment_dict(self, result, email="ana@example.com"):
        return {
            "id": result["appointment_id"],
            "customer_name": result["customer_name"],
            "customer_email": email,
            "service": "Corte",
            "appointment_date": self.valid_date,
            "appointment_time": result["appointment_time"],
            "appointment_end": result["appointment_end"],
        }

    def _make_appointment(self, phone="3838439222", time="09:00", email="ana@example.com"):
        return appointments.create_appointment(
            "Ana Pérez", phone, "Corte", self.valid_date, time, 1, email=email,
        )

    def _failed_confirmation(self, result, email="ana@example.com"):
        apt = self._appointment_dict(result, email=email)
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(
                 notifications.smtplib, "SMTP", return_value=FailingSMTP()
             ):
            sent, reason = send_confirmation_email(1, apt, force=True)
        self.assertFalse(sent)
        return apt


class TestCancelacionSinEfectosColaterales(NotificationsHardeningBase):
    """Cancelación: cambio de estado correcto y NUNCA notificaciones/registros."""

    def test_cancelacion_exitosa_cambia_estado_y_no_notifica(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self.assertEqual(self._log_count(aid), 0)

        ok = appointments.cancel_appointment(
            aid, "3838439222", 1, result["management_token"],
            customer_name="Ana Pérez",
        )
        self.assertTrue(ok)
        status = self._query(
            "SELECT status FROM appointments WHERE id = ?", (aid,)
        )[0]["status"]
        self.assertEqual(status, "cancelled")
        self.assertEqual(self._log_count(aid), 0)

    def test_cancelacion_rechazada_intacta_y_sin_notificacion(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self.assertFalse(
            appointments.cancel_appointment(
                aid, "3838439222", 1, "token-incorrecto",
                customer_name="Ana Pérez",
            )
        )
        status = self._query(
            "SELECT status FROM appointments WHERE id = ?", (aid,)
        )[0]["status"]
        self.assertEqual(status, "confirmed")
        self.assertEqual(self._log_count(aid), 0)

    def test_cancelacion_no_autorizada_rechazada_sin_notificacion(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self.assertFalse(
            appointments.cancel_appointment(
                aid, "3838439222", 1, None, customer_name="Ana Pérez",
            )
        )
        status = self._query(
            "SELECT status FROM appointments WHERE id = ?", (aid,)
        )[0]["status"]
        self.assertEqual(status, "confirmed")
        self.assertEqual(self._log_count(aid), 0)

    def test_cancelacion_turno_inexistente_no_notifica(self):
        self.assertFalse(
            appointments.cancel_appointment(999999, "3838439222", 1, "sometoken")
        )
        self.assertEqual(self._log_count(), 0)


class TestReprogramacionSinEfectosColaterales(NotificationsHardeningBase):
    """Reprogramación: cambio correcto, rechazo sin efectos y sin notificaciones."""

    def test_reprogramacion_exitosa_actualiza_sin_notificacion(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        res = appointments.reschedule_appointment(
            aid, self.valid_date, "10:00", "3838439222", 1,
            result["management_token"], customer_name="Ana Pérez",
        )
        self.assertTrue(res["success"])
        row = self._query(
            "SELECT appointment_time, status FROM appointments WHERE id = ?", (aid,),
        )[0]
        self.assertEqual(row["appointment_time"], "10:00")
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(self._log_count(aid), 0)

    def test_reprogramacion_slot_ocupado_intacta_sin_notificacion(self):
        result = self._make_appointment()
        self._make_appointment(phone="3838439000", time="10:00",
                               email="otro@example.com")
        aid = result["appointment_id"]
        res = appointments.reschedule_appointment(
            aid, self.valid_date, "10:00", "3838439222", 1,
            result["management_token"], customer_name="Ana Pérez",
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "occupied")
        row = self._query(
            "SELECT appointment_time, status FROM appointments WHERE id = ?", (aid,),
        )[0]
        self.assertEqual(row["appointment_time"], "09:00")
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(self._log_count(aid), 0)

    def test_reprogramacion_no_autorizada_intacta_sin_notificacion(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        res = appointments.reschedule_appointment(
            aid, self.valid_date, "11:00", "3838439222", 1,
            "token-incorrecto", customer_name="Ana Pérez",
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "not_found")
        row = self._query(
            "SELECT appointment_time, status FROM appointments WHERE id = ?", (aid,),
        )[0]
        self.assertEqual(row["appointment_time"], "09:00")
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(self._log_count(aid), 0)

    def test_reprogramacion_reintento_no_genera_duplicado_ni_notificacion(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        first = appointments.reschedule_appointment(
            aid, self.valid_date, "12:00", "3838439222", 1,
            result["management_token"], customer_name="Ana Pérez",
        )
        self.assertTrue(first["success"])
        appointments.reschedule_appointment(
            aid, self.valid_date, "12:00", "3838439222", 1,
            result["management_token"], customer_name="Ana Pérez",
        )
        rows = self._query(
            "SELECT COUNT(*) AS n FROM appointments WHERE id = ? AND appointment_time = '12:00'",
            (aid,),
        )
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(self._log_count(aid), 0)


class TestReservaNotificationLog(NotificationsHardeningBase):
    """Creación por API: confirmaciones solo cuando la reserva es exitosa."""

    def _reserve(self, payload):
        return self.client.post("/b/el-corte/api/reservar", json=payload)

    def _payload(self, **extra):
        payload = {
            "nombre": "Ana Pérez",
            "telefono": "3838439222",
            "servicio": "Corte",
            "fecha": self.valid_date,
            "hora": "09:00",
            "email": "ana@example.com",
        }
        payload.update(extra)
        return payload

    def test_reserva_exitosa_genera_confirmacion_y_aviso(self):
        self._enable_notifications("turnos@negocio.com")
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            response = self._reserve(self._payload())

        self.assertEqual(response.status_code, 201)
        aid = response.get_json()["appointment_id"]
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "sent")
        self.assertEqual(
            self._notification_row(aid, BUSINESS_CONFIRMATION)["status"], "sent"
        )
        self.assertEqual(self._log_count(aid), 2)

    def test_reserva_duplicada_una_sola_confirmacion(self):
        self._enable_notifications("turnos@negocio.com")
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            first = self._reserve(self._payload())
            second = self._reserve(self._payload())

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.get_json()["reason"], "occupied")
        self.assertEqual(
            self._query("SELECT COUNT(*) AS n FROM appointments")[0]["n"], 1
        )
        aid = first.get_json()["appointment_id"]
        self.assertEqual(
            self._notification_row(aid, CONFIRMATION)["status"], "sent"
        )
        self.assertEqual(
            self._notification_row(aid, BUSINESS_CONFIRMATION)["status"], "sent"
        )
        self.assertEqual(self._log_count(aid), 2)

    def test_reserva_rechazada_horario_invalido_no_genera_confirmacion(self):
        response = self._reserve(self._payload(hora="25:00"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "invalid_time")
        self.assertEqual(
            self._query("SELECT COUNT(*) AS n FROM appointments")[0]["n"], 0
        )
        self.assertEqual(self._log_count(), 0)

    def test_reserva_ocupada_no_genera_confirmacion(self):
        self._make_appointment()
        response = self._reserve(self._payload())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "occupied")
        self.assertEqual(
            self._query("SELECT COUNT(*) AS n FROM appointments")[0]["n"], 1
        )
        self.assertEqual(self._log_count(), 0)

    def test_reserva_rechazada_por_rollback_no_genera_confirmacion(self):
        response = self._reserve(self._payload(servicio="Servicio-inexistente"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self._query("SELECT COUNT(*) AS n FROM appointments")[0]["n"], 0
        )
        self.assertEqual(self._log_count(), 0)


class TestRetryConsistenteConEstadoDelTurno(NotificationsHardeningBase):
    """Reintento de notificaciones: solo para turnos confirmados y vigentes."""

    def test_retry_no_reenvia_confirmacion_de_turno_cancelado(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self._failed_confirmation(result)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "failed")

        ok = appointments.cancel_appointment(
            aid, "3838439222", 1, result["management_token"],
            customer_name="Ana Pérez",
        )
        self.assertTrue(ok)

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            retried = retry_runner._run_once(business_id=1)

        self.assertEqual(retried, 0)
        self.assertEqual(len(fake.messages), 0)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "failed")

    def test_retry_reenvia_si_el_turno_sigue_confirmado(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self._failed_confirmation(result)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "failed")

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            retried = retry_runner._run_once(business_id=1)

        self.assertEqual(retried, 1)
        self.assertEqual(len(fake.messages), 1)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "sent")

    def test_retry_omite_fila_sin_turno_existente(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self._failed_confirmation(result)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "failed")

        c = database.get_connection()
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("DELETE FROM appointments WHERE id = ?", (aid,))
        c.commit()
        c.close()

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            retried = retry_runner._run_once(business_id=1)

        self.assertEqual(retried, 0)
        self.assertEqual(len(fake.messages), 0)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "failed")

    def test_resend_rechaza_turno_que_no_pertenece_al_negocio(self):
        result = self._make_appointment()
        aid = result["appointment_id"]

        c = database.get_connection()
        c.execute(
            "INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')"
        )
        c.execute(
            """INSERT INTO notification_log
               (appointment_id, business_id, type, channel, destination,
                status, error, last_attempt_at)
               VALUES (?, 2, ?, 'email', 'b@example.com', 'failed', 'smtp down', '')""",
            (aid, CONFIRMATION),
        )
        c.commit()
        c.close()

        row = list_failed_notifications_scoped(business_id=2)[0]
        ok, reason = retry_runner._resend(row)
        self.assertFalse(ok)
        self.assertEqual(reason, "appointment_not_found")
        self.assertEqual(self._log_count(aid), 1)

    def test_retry_concurrente_no_duplica(self):
        result = self._make_appointment()
        aid = result["appointment_id"]
        self._failed_confirmation(result)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "failed")

        barrier = threading.Barrier(2)
        retried_counts = []

        def run():
            barrier.wait()
            retried_counts.append(retry_runner._run_once(business_id=1))

        shared_fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(
                 notifications.smtplib, "SMTP", return_value=shared_fake
             ):
            threads = [
                threading.Thread(target=run),
                threading.Thread(target=run),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(sum(retried_counts), 1)
        self.assertEqual(len(shared_fake.messages), 1)
        self.assertEqual(self._notification_row(aid, CONFIRMATION)["status"], "sent")