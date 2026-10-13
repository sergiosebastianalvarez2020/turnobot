"""Excepción administrativa del cupo de reservas por contacto (Fase 2).

Documenta y verifica el contrato deliberado de
`admin_create_appointment_manual`: la ruta manual pasa
`enforce_contact_limit=False` porque un operador autorizado reserva EN NOMBRE de
un cliente. El cupo anti-abuso sigue activo, sin embargo, en las rutas públicas.

Requisitos verificados:
1. Un admin autorizado crea MÁS de cinco reservas para el mismo contacto por la
   ruta manual, con membresía + CSRF reales.
2. La misma operación por la ruta pública SÍ se bloquea al alcanzar el cupo.
3. La reserva de franjas sigue vigente (no se puede duplicar un horario).
4. El negocio B no hereda las reservas del negocio A (aislamiento).
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database

TELEFONO_SHARED = "3838439222"


class TestExcepcionAdminDelCupo(unittest.TestCase):
    """La ruta manual admin puede exceder el cupo; la pública no."""

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"

        # SQLite aislado, como el resto de los tests de panel admin.
        self._original_db_backend = os.environ.get("DB_BACKEND")
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)
        database.init_database()

        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO businesses (id, name, slug) "
                "VALUES (1, 'El Corte', 'el-corte')"
            )
            # Negocio B para verificar aislamiento.
            connection.execute(
                "INSERT OR IGNORE INTO businesses (id, name, slug) "
                "VALUES (2, 'Otro', 'otro-negocio')"
            )
            connection.commit()
        finally:
            connection.close()

        from database.seed_auth import provision_owner_from_bootstrap

        self._owner_email = "owner@test.local"
        self._owner_password = "testpass123"
        provision_owner_from_bootstrap(
            1, self._owner_email, generate_password_hash(self._owner_password)
        )

        from application import create_app

        self.app = create_app()
        self.client = self.app.test_client()

        self._original_hash = application.ADMIN_PASSWORD_HASH
        self._original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

        login_page = self.client.get("/login")
        self.csrf_login = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        self.client.post(
            "/login",
            data={
                "email": self._owner_email,
                "password": self._owner_password,
                "csrf_token": self.csrf_login,
            },
        )

        # CSRF fresco de la página admin (la sesión se regenera en el login).
        admin_page = self.client.get("/admin")
        match = re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text)
        self.csrf_token = match.group(1) if match else self.csrf_login

        self._fecha = self._proximo_dia_abierto()

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self._original_hash
        application.ADMIN_PASSWORD = self._original_password
        database.DATABASE_PATH = self.original_database_path
        if self._original_db_backend is not None:
            os.environ["DB_BACKEND"] = self._original_db_backend
        else:
            os.environ.pop("DB_BACKEND", None)
        self.temp_dir.cleanup()

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _proximo_dia_abierto(self):
        """Primer día con horarios disponibles (negocio 1)."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from services import appointments

        settings = database.get_business_settings_scoped(1)
        timezone = settings["timezone"] if settings and settings["timezone"] else "UTC"
        hoy = datetime.now(ZoneInfo(timezone)).date()
        for offset in range(1, 30):
            candidato = hoy + timedelta(days=offset)
            slots = appointments.get_available_slots(candidato.isoformat(), 1, service="Corte")
            if slots:
                return candidato.isoformat(), slots[0]
        raise AssertionError("No hay días abiertos en el seed.")

    def _crear_admin(self, hora, telefono=TELEFONO_SHARED, csrf=None):
        return self.client.post(
            "/admin/turnos/crear",
            data={
                "csrf_token": csrf or self.csrf_token,
                "customer_name": "Cliente Admin",
                "phone": telefono,
                "service_name": "Corte",
                "appointment_date": self._fecha[0],
                "appointment_time": hora,
            },
            headers={"Accept": "application/json"},
        )

    def _reservar_publico(self, hora, telefono=TELEFONO_SHARED):
        return self.client.post(
            "/api/reservar",
            json={
                "nombre": "Cliente Publico",
                "telefono": telefono,
                "servicio": "Corte",
                "fecha": self._fecha[0],
                "hora": hora,
            },
        )

    def _confirmados_del_contacto(self, business_id=1, telefono=TELEFONO_SHARED):
        connection = database.get_connection()
        try:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM appointments "
                "WHERE business_id = ? AND phone = ? AND status = 'confirmed'",
                (business_id, telefono),
            ).fetchone()["n"]
        finally:
            connection.close()

    # ------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------

    def test_admin_crea_mas_del_limite_para_el_mismo_contacto(self):
        """La excepción admin permite superar el cupo que frena al público."""
        horarios = self._franjas_libres(8)

        for hora in horarios:
            response = self._crear_admin(hora)
            self.assertEqual(
                response.status_code, 200, f"Falló el alta manual en {hora}: {response.data}"
            )
            self.assertTrue(response.get_json()["success"])

        self.assertEqual(
            self._confirmados_del_contacto(),
            len(horarios),
            "El admin debe poder crear más turnos que el límite público.",
        )
        self.assertGreater(len(horarios), 5)

    def test_ruta_publica_sigue_bloqueando_el_mismo_contacto(self):
        """Con el cupo alcanzado, la ruta pública responde 429 CONTACT_LIMIT."""
        horarios = self._franjas_libres(8)
        for hora in horarios:
            self._crear_admin(hora)

        respuesta = self._reservar_publico(self._franjas_libres(1)[0])
        self.assertEqual(respuesta.status_code, 429)
        data = respuesta.get_json()
        self.assertEqual(data["code"], "CONTACT_LIMIT")
        self.assertNotIn(TELEFONO_SHARED, data["error"])

    def test_admin_no_duplica_una_franja_ocupada(self):
        """La excepción del cupo no desactiva la reserva de franjas."""
        primera = self._crear_admin("09:00")
        self.assertTrue(primera.get_json()["success"])

        segunda = self._crear_admin("09:00")
        self.assertFalse(segunda.get_json()["success"])
        self.assertEqual(
            self._confirmados_del_contacto(),
            1,
            "Una franja ocupada no debe generar un segundo turno.",
        )

    def test_sin_csrf_no_crea(self):
        """La autorización no se debilita: sin CSRF válido no hay alta."""
        antes = self._confirmados_del_contacto(telefono="3838000001")
        respuesta = self._crear_admin("09:00", telefono="3838000001", csrf="token-invalido")
        self.assertFalse(respuesta.get_json()["success"])
        self.assertEqual(self._confirmados_del_contacto(telefono="3838000001"), antes)

    def test_aislamiento_entre_negocios(self):
        """El negocio B tiene su propio cupo y sus propias franjas."""
        horarios_a = self._franjas_libres(6)
        for hora in horarios_a:
            self._crear_admin(hora)

        self.assertEqual(self._confirmados_del_contacto(business_id=1), len(horarios_a))
        self.assertEqual(
            self._confirmados_del_contacto(business_id=2),
            0,
            "Las reservas del negocio 1 no deben aparecer en el negocio 2.",
        )

    def _franjas_libres(self, cantidad):
        """Horarios disponibles y consecutivos para el día elegido."""
        from services import appointments

        slots = appointments.get_available_slots(self._fecha[0], 1, service="Corte")
        return slots[:cantidad]


if __name__ == "__main__":
    unittest.main()
