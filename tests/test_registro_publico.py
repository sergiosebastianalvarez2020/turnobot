"""Pruebas de Etapa 12: registro público de negocios.

Valida que un usuario anónimo puede solicitar el alta de un negocio
sin ser superadmin, reutilizando la infraestructura existente de
provision_business(), invitations, emails y rate limiting.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as application
import database.database as database
from database.database import get_connection
from services import platform as platform_service
from services import notifications


SMTP_ENV = {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587",
            "EMAIL_FROM": "no-reply@test.com", "SMTP_USER": "user@test.com"}


class FakeSMTP:
    def __init__(self):
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self):
        return (220, b"ok")

    def login(self, user, password):
        return

    def sendmail(self, from_addr, to_addrs, msg):
        self.messages.append((from_addr, to_addrs, msg))


class RegistroPublicoBase(unittest.TestCase):

    OWNER_EMAIL = "dueno@test-registro.com"

    def setUp(self):
        application.rate_limit_state.clear()
        application.app.config["TESTING"] = True
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "registro.db"
        database.init_database()
        self.client = application.app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_db
        self.tmp.cleanup()

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

    def _get_registro_form(self):
        page = self.client.get("/registro")
        return page

    def _post_registro(self, business_name="Mi Negocio", slug=None,
                       owner_email=OWNER_EMAIL, csrf=None):
        if csrf is None:
            page = self._get_registro_form()
            csrf = self._csrf_from(page)
        data = {
            "business_name": business_name,
            "owner_email": owner_email,
            "csrf_token": csrf,
        }
        if slug is not None:
            data["slug"] = slug
        return self.client.post("/registro", data=data)


class TestRegistroPublico(RegistroPublicoBase):

    def test_get_registro_devuelve_200(self):
        page = self._get_registro_form()
        self.assertEqual(page.status_code, 200)
        self.assertIn("Solicitar alta de negocio", page.text)

    def test_get_registro_sent_muestra_confirmacion(self):
        page = self.client.get("/registro?sent=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Solicitud recibida", page.text)

    def test_registro_exitoso_crea_negocio_pending(self):
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=FakeSMTP()):
            response = self._post_registro(business_name="Cafe Luna")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/registro?sent=1", response.headers["Location"])

        rows = self._query(
            "SELECT active, pending, slug FROM businesses WHERE slug = 'cafe-luna'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active"], 0)
        self.assertEqual(rows[0]["pending"], 1)

    def test_registro_exitoso_envia_email_al_owner(self):
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            self._post_registro(business_name="Café Luna",
                                owner_email="dueno@cafe-luna.com")
        owner_emails = [msg for _from, to, msg in fake.messages
                        if "dueno@cafe-luna.com" in to]
        self.assertEqual(len(owner_emails), 1)

    def test_registro_exitoso_envia_email_al_superadmin(self):
        platform_service.create_superadmin(
            "admin@test-registro.com", "s3cr3t-strong-pass-1", "admin@test-registro.com"
        )
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=fake):
            self._post_registro(business_name="Café Luna")
        sa_emails = [msg for _from, to, msg in fake.messages
                     if "admin@test-registro.com" in to]
        self.assertEqual(len(sa_emails), 1)

    def test_registro_requiere_csrf_valido(self):
        response = self.client.post("/registro", data={
            "business_name": "Test", "slug": "", "owner_email": "test@x.com",
            "csrf_token": "invalid-token-12345",
        })
        self.assertEqual(response.status_code, 400)

    def test_rate_limiting_bloquea_despues_del_limite(self):
        for i in range(application.REGISTRO_REQUEST_LIMIT):
            response = self._post_registro(business_name="Negocio",
                                           owner_email=f"owner{i}@x.com")
            self.assertEqual(response.status_code, 302)
        blocked = self._post_registro(business_name="Negocio",
                                       owner_email="blocked@x.com")
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("error", blocked.headers["Location"])

    def test_validacion_nombre_obligatorio(self):
        response = self._post_registro(business_name="", owner_email="ok@x.com")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])
        self.assertIn("obligatorio", response.headers["Location"])

    def test_validacion_email_invalido(self):
        response = self._post_registro(business_name="Test", owner_email="no-es-email")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])
        self.assertIn("email", response.headers["Location"])

    def test_registro_no_requiere_superadmin(self):
        page = self._get_registro_form()
        self.assertEqual(page.status_code, 200)
        self.assertIn("Solicitar alta de negocio", page.text)

    def test_registro_no_revela_detalles_internos_en_error(self):
        response = self._post_registro(business_name="Test", owner_email="bad-email")
        location = response.headers["Location"]
        self.assertNotIn("Traceback", location)
        self.assertNotIn("sqlite3", location)
        self.assertNotIn("SQL", location)

    def test_registro_reutiliza_business_id_de_provision(self):
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=FakeSMTP()), \
             mock.patch.object(notifications, "send_apply_notice_to_superadmin",
                               wraps=notifications.send_apply_notice_to_superadmin) as mock_notice:
            response = self._post_registro(business_name="Test Registro",
                                           owner_email="prop@test-registro.com")

        self.assertEqual(response.status_code, 302)

        notice_call = mock_notice.call_args
        self.assertIsNotNone(notice_call, "send_apply_notice_to_superadmin no fue llamado")

        db_row = self._query(
            "SELECT id FROM businesses WHERE slug = 'test-registro' ORDER BY id DESC LIMIT 1"
        )
        self.assertTrue(db_row, "El negocio no fue creado en DB")
        db_business_id = db_row[0]["id"]

        notice_business_id = notice_call[0][1]
        self.assertEqual(notice_business_id, db_business_id)

    def test_negocio_con_slug_duplicado_falla(self):
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=FakeSMTP()):
            self._post_registro(business_name="Primero", slug="slug-test",
                                owner_email="a@x.com")
        response = self._post_registro(business_name="Segundo", slug="slug-test",
                                       owner_email="b@x.com")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])

    def test_email_duplicado_no_genera_500(self):
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False), \
             mock.patch.object(notifications.smtplib, "SMTP", return_value=FakeSMTP()):
            self._post_registro(business_name="Primero",
                                owner_email="duplicado@test-registro.com")
        response = self._post_registro(business_name="Segundo",
                                       owner_email="duplicado@test-registro.com")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
