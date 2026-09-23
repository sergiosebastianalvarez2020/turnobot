"""Etapa 4 - Infraestructura de emails (EMAIL 1..5) y fiabilidad de envíos.

Cubre, por requisito:

- EMAIL 1: solicitud de alta => confirmación al solicitante + aviso a superadmines;
  NO auto-activa el negocio ni genera invitación.
- EMAIL 2: aprobación => invitación (un solo uso, vence 72h) SOLO tras aprobar.
- EMAIL 4: confirmación al cliente con gestión (cancelar/reprogramar).
- EMAIL 5: aviso de reserva al email del negocio (scoped por tenant).
- Fiabilidad: reserva se guarda primero, el email no la revierte; estado
  pending/sent/failed; reintento por script; idempotencia por unique.
- Seguridad: sin secretos ni tokens en logs, headers sanitizados (CRLF),
  aislamiento entre tenants.
"""

import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

import app as application
import database.database as database
import scripts.retry_failed_notifications as retry_runner
from database.database import get_connection
from services import appointments, notifications
from services import platform as platform_service
from services.notifications import (
    BUSINESS_CONFIRMATION,
    CONFIRMATION,
    dispatch_booking_emails,
    send_business_confirmation_email,
    send_confirmation_email,
)

SMTP_ENV = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587", "EMAIL_FROM": "no-reply@test.com"}


def _decode_mime_text(msg):
    parsed = BytesParser(policy=policy.default).parsebytes(str(msg).encode("utf-8"))
    for part in parsed.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content() or ""
    return str(msg)


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
    """Emula un fallo SMTP cuyo mensaje contiene la contraseña (para probar
    que `_safe_error` la redacta antes de persistirla)."""

    def sendmail(self, from_addr, to_addrs, msg):
        raise ConnectionRefusedError(
            "authentication failed for " + os.environ.get("SMTP_PASSWORD", "secret")
        )


def _next_open_day(base=None):
    date = (base or datetime.now().date()) + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


class EmailBase(unittest.TestCase):
    SUPERADMIN_EMAIL = "admin@tu-dominio.com"
    SUPERADMIN_PASSWORD = "s3cr3t-strong-pass"
    OWNER_EMAIL = "dueno@aurora.com"

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.valid_date = _next_open_day()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    @staticmethod
    def _query(sql, params=None):
        c = get_connection()
        try:
            return c.execute(sql, params or ()).fetchall()
        finally:
            c.close()

    @staticmethod
    def _csrf_from(page):
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        return match.group(1) if match else None

    def _enable_notifications(self, business_id=1, email="turnos@negocio.com"):
        c = get_connection()
        c.execute(
            """UPDATE business_settings
               SET notifications_enabled = 1, notification_email = ?
               WHERE business_id = ?""",
            (email, business_id),
        )
        c.commit()
        c.close()


class EmailPlataformaHighLevel(EmailBase):
    """End-to-end del flujo de alta por HTTP (EMAIL 1 y EMAIL 2)."""

    def _login_superadmin(self):
        platform_service.create_superadmin(
            self.SUPERADMIN_EMAIL, self.SUPERADMIN_PASSWORD, self.SUPERADMIN_EMAIL
        )
        page = self.client.get("/superadmin/login")
        csrf = self._csrf_from(page)
        return self.client.post(
            "/superadmin/login",
            data={
                "email": self.SUPERADMIN_EMAIL,
                "password": self.SUPERADMIN_PASSWORD,
                "csrf_token": csrf,
            },
        )

    def _provision(self):
        page = self.client.get("/superadmin")
        csrf = self._csrf_from(page)
        return self.client.post(
            "/superadmin/negocios/crear",
            data={
                "nombre": "Cafetería Aurora",
                "slug": "cafetera-aurora",
                "owner_email": self.OWNER_EMAIL,
                "csrf_token": csrf,
            },
        )

    def _approve(self):
        page = self.client.get("/superadmin/negocios/2")
        csrf = self._csrf_from(page)
        return self.client.post("/superadmin/negocios/2/aprobar", data={"csrf_token": csrf})

    def _send(self, response, fake):
        sent = [(to, _decode_mime_text(msg)) for _from, to, msg in fake.messages]
        return sent

    def test_email1_solicitud_confirma_al_solicitante_y_notifica_al_superadmin(self):
        self._login_superadmin()
        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            response = self._provision()
        self.assertEqual(response.status_code, 302)

        # El solicitante recibe confirmación de recepción (EMAIL 1).
        owner_emails = [m for to, m in self._send(response, fake) if self.OWNER_EMAIL in to[0]]
        self.assertEqual(len(owner_emails), 1)
        self.assertIn("recibimos tu solicitud", owner_emails[0].lower())
        self.assertNotIn("configurar mi contraseña", owner_emails[0].lower())

        # El superadmin activo recibe el aviso de alta pendiente (EMAIL 1).
        sa_emails = [m for to, m in self._send(response, fake) if self.SUPERADMIN_EMAIL in to[0]]
        self.assertEqual(len(sa_emails), 1)
        self.assertIn("pendiente", sa_emails[0].lower())

        # Sin auto-activar y sin invitación.
        row = self._query("SELECT active, pending FROM businesses WHERE slug = 'cafetera-aurora'")[
            0
        ]
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["pending"], 1)
        invites = self._query(
            """SELECT COUNT(*) AS n FROM invitations i
               JOIN businesses b ON b.id = i.business_id
               WHERE b.slug = 'cafetera-aurora'"""
        )[0]["n"]
        self.assertEqual(invites, 0)

    def test_email2_invitacion_solo_despues_de_aprobar(self):
        self._login_superadmin()
        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            self._provision()
            # Tras crear el owner recibe EMAIL 1 (recepción), pero NUNCA EMAIL 2.
            owner_emails = [
                _decode_mime_text(msg) for _f, to, msg in fake.messages if self.OWNER_EMAIL in to[0]
            ]
            self.assertGreaterEqual(len(owner_emails), 1)
            self.assertNotIn("/invitacion/", owner_emails[0])

            self._approve()
            owner_emails = [
                _decode_mime_text(msg) for _f, to, msg in fake.messages if self.OWNER_EMAIL in to[0]
            ]
            self.assertEqual(len(owner_emails), 2)  # EMAIL 1 + EMAIL 2
            invitation_email = owner_emails[1]
            self.assertIn("aprobado", invitation_email.lower())
            self.assertIn("/invitacion/", invitation_email)

        row = self._query("SELECT active, pending FROM businesses WHERE slug = 'cafetera-aurora'")[
            0
        ]
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["pending"], 0)

    def test_email2_reenvio_genera_token_nuevo(self):
        self._login_superadmin()
        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            self._provision()
            self._approve()
            page = self.client.get("/superadmin/negocios/2")
            csrf = self._csrf_from(page)
            self.client.post("/superadmin/negocios/2/reinviar", data={"csrf_token": csrf})

        owner_emails = [
            _decode_mime_text(msg) for _f, to, msg in fake.messages if self.OWNER_EMAIL in to[0]
        ]
        # EMAIL 1 (recepción) + EMAIL 2 (aprobación) + EMAIL 2 (reenvío).
        invitation_emails = [m for m in owner_emails if "/invitacion/" in m]
        self.assertEqual(len(invitation_emails), 2)
        tokens = set(
            re.search(r"/b/[a-z0-9-]+/invitacion/([A-Za-z0-9_-]+)", m).group(1)
            for m in invitation_emails
        )
        self.assertEqual(len(tokens), 2)


class EmailReservas(EmailBase):
    """EMAIL 4 + EMAIL 5, fiabilidad, sanitización e idempotencia."""

    def _appointment_dict(self, result, email="ana@example.com"):
        return {
            "id": result["appointment_id"],
            "customer_name": "Ana Pérez",
            "customer_email": email,
            "service": "Corte",
            "appointment_date": self.valid_date,
            "appointment_time": "09:00",
            "appointment_end": "09:30",
        }

    def _notification_row(self, appointment_id, type_):
        rows = self._query(
            """SELECT status, error, destination FROM notification_log
               WHERE appointment_id = ? AND type = ?""",
            (appointment_id, type_),
        )
        return rows[0] if rows else None

    def test_reserva_confirma_al_cliente_y_avisa_al_negocio(self):
        self._enable_notifications(1, "turnos@negocio.com")
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="ana@example.com"
        )
        self.assertTrue(result["success"])
        apt = self._appointment_dict(result)

        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            outcome = dispatch_booking_emails(
                1, apt, slug="elcorte", management_token="tok-x", public_base_url="https://x.com"
            )

        self.assertTrue(outcome["confirmation"][0])
        self.assertTrue(outcome["business"][0])
        recipients = {tuple(to) for _f, to, _m in fake.messages}
        self.assertIn(("ana@example.com",), recipients)
        self.assertIn(("turnos@negocio.com",), recipients)
        self.assertEqual(len(fake.messages), 2)

        # EMAIL 4 incluye gestión para cancelar/reprogramar.
        customer_text = _decode_mime_text(
            next(m for _f, to, m in fake.messages if "ana@example.com" in to)
        )
        self.assertIn("cancelar o reprogramar", customer_text.lower())
        self.assertIn(
            "/b/elcorte/turno/tok-x?id={}".format(result["appointment_id"]), customer_text
        )

        # EMAIL 5 muestra cliente/servicio/fecha.
        business_text = _decode_mime_text(
            next(m for _f, to, m in fake.messages if "turnos@negocio.com" in to)
        )
        self.assertIn("Ana Pérez", business_text)
        self.assertIn("Corte", business_text)

        self.assertEqual(
            self._notification_row(result["appointment_id"], CONFIRMATION)["status"], "sent"
        )
        self.assertEqual(
            self._notification_row(result["appointment_id"], BUSINESS_CONFIRMATION)["status"],
            "sent",
        )

    def test_sin_email_de_negocio_el_aviso_no_impide_la_confirmacion(self):
        self._enable_notifications(1, "")
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "10:00", 1, email="ana@example.com"
        )
        apt = self._appointment_dict(result)

        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            outcome = dispatch_booking_emails(
                1, apt, slug="elcorte", public_base_url="https://x.com"
            )

        self.assertTrue(outcome["confirmation"][0])
        self.assertFalse(outcome["business"][0])
        self.assertEqual(outcome["business"][1], "no_business_destination")
        self.assertEqual(len(fake.messages), 1)

    def test_email5_scoped_por_tenant_no_filtra_otro_negocio(self):
        # Negocio 2 tiene su propio email; el aviso del negocio 1 jamás lo usa.
        database.create_business_with_owner(
            "Otro Negocio", "otro@x.com", password="clave-muy-segura-123", slug="otro"
        )
        self._enable_notifications(1, "a@x.com")
        self._enable_notifications(2, "b@y.com")

        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "11:00", 1, email="ana@example.com"
        )
        apt = self._appointment_dict(result)

        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            ok, reason = send_business_confirmation_email(1, apt)

        self.assertTrue(ok)
        self.assertIsNone(reason)
        tos = {to[0] for _f, to, _m in fake.messages}
        self.assertEqual(tos, {"a@x.com"})
        self.assertNotIn("b@y.com", tos)

    def test_envio_fallido_no_revierte_la_reserva_y_queda_failed(self):
        self._enable_notifications(1, "turnos@negocio.com")
        fake = FailingSMTP()
        env = dict(SMTP_ENV)
        env["SMTP_PASSWORD"] = "muy-secreta-clave-xyz"
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            response = self.client.post(
                "/b/el-corte/api/reservar",
                json={
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": self.valid_date,
                    "hora": "09:00",
                    "email": "ana@example.com",
                },
            )

        # La reserva SE GUARDÓ (201) aunque los emails fallaron.
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data["success"])
        appointment_id = data["appointment_id"]

        row_confirm = self._notification_row(appointment_id, CONFIRMATION)
        row_business = self._notification_row(appointment_id, BUSINESS_CONFIRMATION)
        self.assertEqual(row_confirm["status"], "failed")
        self.assertEqual(row_business["status"], "failed")

        # El error NO contiene el secreto SMTP (redactado) ni CRLF.
        for row in (row_confirm, row_business):
            self.assertIn("REDACTED", row["error"])
            self.assertNotIn("muy-secreta-clave-xyz", row["error"])
            self.assertNotIn("\n", row["error"])

        # El turno existe y sigue confirmado.
        self.assertEqual(
            self._query("SELECT status FROM appointments WHERE id = ?", (appointment_id,))[0][
                "status"
            ],
            "confirmed",
        )

    def test_reintento_de_failed_pasa_a_sent_y_no_duplica(self):
        self._enable_notifications(1, "turnos@negocio.com")
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="ana@example.com"
        )
        apt = self._appointment_dict(result)

        fake_bad = FailingSMTP()
        env = dict(SMTP_ENV)
        env["SMTP_PASSWORD"] = "muy-secreta-clave-xyz"
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake_bad),
        ):
            self.assertFalse(dispatch_booking_emails(1, apt)["confirmation"][0])
            self.assertFalse(dispatch_booking_emails(1, apt)["business"][0])

        self.assertEqual(
            self._notification_row(result["appointment_id"], CONFIRMATION)["status"], "failed"
        )

        # El runner reintenta y acierta (SMTP bien configurado ahora).
        fake_ok = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake_ok),
        ):
            retried = retry_runner._run_once(business_id=1)

        self.assertEqual(retried, 2)
        self.assertEqual(len(fake_ok.messages), 2)
        self.assertEqual(
            self._notification_row(result["appointment_id"], CONFIRMATION)["status"], "sent"
        )
        self.assertEqual(
            self._notification_row(result["appointment_id"], BUSINESS_CONFIRMATION)["status"],
            "sent",
        )

        # Un segundo intento no vuelve a enviar (idempotencia).
        fake_ok2 = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake_ok2),
        ):
            retried2 = retry_runner._run_once(business_id=1)
        self.assertEqual(retried2, 0)
        self.assertEqual(len(fake_ok2.messages), 0)

    def test_sin_smtp_falla_temprano_y_no_registra(self):
        env = {k: v for k, v in os.environ.items() if k != "SMTP_HOST"}
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "12:00", 1, email="ana@example.com"
        )
        with mock.patch.dict(os.environ, env, clear=True):
            sent, reason = send_confirmation_email(1, self._appointment_dict(result), force=True)
        self.assertFalse(sent)
        self.assertEqual(reason, "smtp_not_configured")
        self.assertEqual(
            self._query(
                "SELECT COUNT(*) AS n FROM notification_log WHERE appointment_id = ?",
                (result["appointment_id"],),
            )[0]["n"],
            0,
        )


class EmailSeguridad(EmailBase):
    def test_subject_sanitiza_crlf_no_inyecta_headers(self):
        # El nombre del negocio (origen del subject) no puede inyectar
        # cabeceras nuevas en el email.
        connection = database.get_connection()
        connection.execute(
            "UPDATE business_settings SET notifications_enabled = 1, business_name = ? WHERE business_id = 1",
            ("El Corte\r\nBcc: evil@example.com",),
        )
        connection.commit()
        connection.close()

        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", self.valid_date, "09:00", 1, email="ana@example.com"
        )
        apt = {
            "id": result["appointment_id"],
            "customer_name": "Ana Pérez",
            "customer_email": "ana@example.com",
            "service": "Corte",
            "appointment_date": self.valid_date,
            "appointment_time": "09:00",
            "appointment_end": "09:30",
        }
        fake = FakeSMTP()
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
        ):
            ok, reason = send_confirmation_email(1, apt, force=True)
        self.assertTrue(ok)

        parsed = BytesParser(policy=policy.default).parsebytes(
            str(fake.messages[0][2]).encode("utf-8")
        )
        names = [name for name, _ in parsed.raw_items()]
        # El CRLF del nombre se convirtió en espacios DENTRO del asunto: no se
        # creó ninguna cabecera nueva (header injection bloqueada).
        self.assertNotIn("Bcc", names)
        self.assertIn("Subject", names)

    def test_logs_no_contienen_tokens_de_invitacion(self):
        # EMAIL 2 nunca loguea el token: el mensaje de la ruta de aprobación
        # contiene el enlace (para el superadmin) pero el LOG no.
        platform_service.create_superadmin(
            self.SUPERADMIN_EMAIL, self.SUPERADMIN_PASSWORD, self.SUPERADMIN_EMAIL
        )
        page = self.client.get("/superadmin/login")
        csrf = self._csrf_from(page)
        self.client.post(
            "/superadmin/login",
            data={
                "email": self.SUPERADMIN_EMAIL,
                "password": self.SUPERADMIN_PASSWORD,
                "csrf_token": csrf,
            },
        )

        fake = FakeSMTP()
        invitation_links = []
        with (
            mock.patch.dict(os.environ, SMTP_ENV, clear=False),
            mock.patch.object(notifications.smtplib, "SMTP", return_value=fake),
            mock.patch.object(
                application,
                "send_approved_invitation_email",
                side_effect=lambda *args, **kwargs: (
                    invitation_links.append(args[2]) or (True, None)
                ),
            ),
            mock.patch.object(application.logger, "error") as log_error,
            mock.patch.object(application.logger, "exception") as log_exc,
            mock.patch.object(application.logger, "info"),
            mock.patch.object(notifications.logger, "error"),
        ):
            page = self.client.get("/superadmin")
            csrf = self._csrf_from(page)
            self.client.post(
                "/superadmin/negocios/crear",
                data={
                    "nombre": "Cafetería Aurora",
                    "slug": "cafetera-aurora",
                    "owner_email": self.OWNER_EMAIL,
                    "csrf_token": csrf,
                },
            )
            page = self.client.get("/superadmin/negocios/2")
            csrf = self._csrf_from(page)
            response = self.client.post("/superadmin/negocios/2/aprobar", data={"csrf_token": csrf})

        token = re.search(
            r"/b/cafetera-aurora/invitacion/([A-Za-z0-9_-]+)", response.headers.get("Location", "")
        )
        self.assertIsNone(token)
        token = re.search(r"invitacion/([A-Za-z0-9_-]+)", " ".join(invitation_links))
        self.assertIsNotNone(token)

        call_logs = " ".join(
            str(c)
            for call in list(log_error.call_args_list) + list(log_exc.call_args_list)
            for c in (call,)
        )
        self.assertNotIn(token.group(1), call_logs)
        # Y tampoco en la auditoría de plataforma ni en el detalle del negocio.
        audit = self._query("SELECT detail FROM audit_log")
        blob = " ".join(row["detail"] for row in audit)
        self.assertNotIn(token.group(1), blob)
        detail_page = self.client.get("/superadmin/negocios/2")
        self.assertNotIn(token.group(1), detail_page.text)

    def test_editar_settings_guarda_notification_email(self):
        connection = database.get_connection()
        connection.execute(
            """UPDATE business_settings
               SET notifications_enabled = 1, notification_email = 'turnos@minelpelo.com'
               WHERE business_id = 1"""
        )
        connection.commit()
        connection.close()
        settings = database.get_business_settings_scoped(1)
        self.assertEqual(settings["notification_email"], "turnos@minelpelo.com")
        self.assertEqual(settings["notifications_enabled"], 1)


if __name__ == "__main__":
    unittest.main()
