"""Pruebas de fidelización por puntos (MVP Etapa 10.1).

Cubre (según ETAPA10_DISENO_FIDELIZACION.md sección 12 y decisión de identidad):
- Configuración (off por defecto, activar/desactivar, valores válidos/inválidos).
- Acreditación (completed sí; confirmed/cancelled/no_show no; fidelización off no).
- Idempotencia (reproceso y reintentos => una sola acreditación).
- Ledger (EARN con appointment, business_id, actor NULL).
- Ajustes (suma, resta, motivo obligatorio, actor, saldo nunca negativo).
- Multi-tenant (A no ve/modifica B; appointment de B no acredita en A).
- Estados (sticky award).
- Normalización (teléfonos con formatos distintos => misma cuenta).
"""

import re
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection
from services import loyalty


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


def _insert_confirmed_appointment(business_id, date_, time_, phone="3815000001", name="Cliente", email=None):
    end_min = (int(time_[:2]) * 60 + int(time_[3:])) + 60
    end_hhmm = f"{end_min // 60:02d}:{end_min % 60:02d}"
    c = get_connection()
    try:
        cur = c.execute(
            "INSERT INTO appointments "
            "(customer_name, phone, customer_email, service, appointment_date, "
            " appointment_time, appointment_end, duration, status, business_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 60, 'confirmed', ?)",
            (name, phone, email or None, "Corte", date_, time_, end_hhmm, business_id),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


class LoyaltyBase(unittest.TestCase):
    """Negocio 1 con login admin. Helpers para crear/completar turnos."""

    valid_date = None

    @classmethod
    def setUpClass(cls):
        cls.valid_date = _next_open_day()

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        # Business B para pruebas de aislamiento multi-tenant.
        self._execute("INSERT INTO businesses (id, name, slug) VALUES (2, 'Business B', 'business-b')")

        self.client = application.app.test_client()
        # Usuario real como actor de ajustes (FK points_ledger.actor_user_id -> users.id).
        c = get_connection()
        try:
            cur = c.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("actor@test.local", generate_password_hash("actor")),
            )
            c.commit()
            self.actor_user_id = cur.lastrowid
        finally:
            c.close()

        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None

        login_page = self.client.get("/login")
        self.csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"', login_page.text
        ).group(1)
        self.client.post(
            "/login",
            data={"password": "correcta", "csrf_token": self.csrf_token},
        )

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    # ---- helpers ----------------------------------------------------------

    def _execute(self, sql, params=()):
        c = get_connection()
        try:
            c.execute(sql, params)
            c.commit()
        finally:
            c.close()

    def _create_turno(self, business_id=1, phone="3815000001", name="Cliente", email=None):
        return _insert_confirmed_appointment(
            business_id, self.valid_date, "10:00",
            phone=phone, name=name, email=email,
        )

    def _complete(self, appointment_id, business_id=1):
        database.update_appointment_status_scoped(appointment_id, "completed", business_id)
        return loyalty.award_points_for_completed(business_id, appointment_id)

    def _account(self, business_id, phone):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT * FROM loyalty_accounts WHERE business_id = ? AND customer_phone = ?",
                (business_id, phone),
            ).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def _ledger(self, business_id, account_id):
        return database.list_points_ledger_scoped(business_id, account_id)


class TestLoyaltyConfiguration(LoyaltyBase):

    def test_fidelizacion_off_por_defecto(self):
        settings = database.ensure_loyalty_settings_scoped(1)
        self.assertIsNotNone(settings)
        self.assertEqual(settings["enabled"], 0)
        self.assertEqual(settings["points_per_completed_appointment"], 1)

    def test_activar_y_desactivar(self):
        ok = database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=3)
        self.assertTrue(ok)
        self.assertEqual(database.get_loyalty_settings_scoped(1)["enabled"], 1)
        self.assertEqual(database.get_loyalty_settings_scoped(1)["points_per_completed_appointment"], 3)

        ok = database.update_loyalty_settings_scoped(1, enabled=False, points_per_completed_appointment=1)
        self.assertTrue(ok)
        self.assertEqual(database.get_loyalty_settings_scoped(1)["enabled"], 0)

    def test_rechazo_de_puntos_negativos(self):
        ok = database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=-5)
        self.assertFalse(ok)

    def test_rechazo_de_valor_no_entero(self):
        ok = database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment="abc")
        self.assertFalse(ok)
class TestLoyaltyAcreditacion(LoyaltyBase):

    def test_completed_genera_puntos(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        res = self._complete(apt, 1)
        self.assertTrue(res["success"])
        self.assertEqual(res["reason"], "awarded")
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 2)

    def test_confirmed_no_genera(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        res = loyalty.award_points_for_completed(1, apt)
        self.assertEqual(res["reason"], "not_completed")
        self.assertIsNone(self._account(1, "3815000001"))

    def test_cancelled_no_genera(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        database.update_appointment_status_scoped(apt, "cancelled", 1)
        res = loyalty.award_points_for_completed(1, apt)
        self.assertEqual(res["reason"], "not_completed")
        self.assertIsNone(self._account(1, "3815000001"))

    def test_no_show_no_genera(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        database.update_appointment_status_scoped(apt, "no_show", 1)
        res = loyalty.award_points_for_completed(1, apt)
        self.assertEqual(res["reason"], "not_completed")
        self.assertIsNone(self._account(1, "3815000001"))

    def test_fidelizacion_off_no_genera(self):
        apt = self._create_turno()
        database.update_appointment_status_scoped(apt, "completed", 1)
        res = loyalty.award_points_for_completed(1, apt)
        self.assertEqual(res["reason"], "disabled")
        self.assertIsNone(self._account(1, "3815000001"))


class TestLoyaltyIdempotencia(LoyaltyBase):

    def test_mismo_turno_procesado_dos_veces(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=5)
        apt = self._create_turno()
        first = self._complete(apt, 1)
        second = self._complete(apt, 1)
        self.assertEqual(first["reason"], "awarded")
        self.assertEqual(second["reason"], "already_awarded")
        account = self._account(1, "3815000001")
        self.assertEqual(account["points_balance"], 5)
        earns = [m for m in self._ledger(1, account["id"]) if m["type"] == "earn"]
        self.assertEqual(len(earns), 1)

    def test_doble_acreditacion_por_http_no_duplica(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        for _ in range(2):
            self.client.post(
                f"/admin/turnos/{apt}/estado",
                data={"csrf_token": self.csrf_token, "status": "completed"},
            )
        account = self._account(1, "3815000001")
        self.assertEqual(account["points_balance"], 2)
        earns = [m for m in self._ledger(1, account["id"]) if m["type"] == "earn"]
        self.assertEqual(len(earns), 1)


class TestLoyaltyLedger(LoyaltyBase):

    def test_movimiento_earn_registrado(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=3)
        apt = self._create_turno(email="Cliente@example.com")
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        ledger = self._ledger(1, account["id"])
        self.assertEqual(len(ledger), 1)
        mv = ledger[0]
        self.assertEqual(mv["type"], "earn")
        self.assertEqual(mv["delta"], 3)
        self.assertEqual(mv["business_id"], 1)
        self.assertEqual(mv["appointment_id"], apt)
        self.assertIsNone(mv["actor_user_id"])

    def test_saldo_reconstruible_desde_ledger(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        phone = "3815000001"
        apt_a = self._create_turno(phone=phone)
        self._complete(apt_a, 1)
        apt_b = _insert_confirmed_appointment(1, self.valid_date, "11:00", phone=phone)
        self._complete(apt_b, 1)
        account = self._account(1, phone)
        ledger = self._ledger(1, account["id"])
        self.assertEqual(sum(m["delta"] for m in ledger), account["points_balance"])
        self.assertEqual(account["points_balance"], 4)
class TestLoyaltyAjustes(LoyaltyBase):

    def test_suma_manual(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        res = loyalty.adjust_points(1, account["id"], "+10", "cortesía", actor_user_id=self.actor_user_id)
        self.assertTrue(res["success"])
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 12)

    def test_resta_manual(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=10)
        apt = self._create_turno()
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        res = loyalty.adjust_points(1, account["id"], -4, "reclamo", actor_user_id=self.actor_user_id)
        self.assertTrue(res["success"])
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 6)

    def test_motivo_obligatorio(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        res = loyalty.adjust_points(1, account["id"], 5, "", actor_user_id=self.actor_user_id)
        self.assertFalse(res["success"])
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 2)

    def test_actor_registrado(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        loyalty.adjust_points(1, account["id"], 5, "cortesía", actor_user_id=self.actor_user_id)
        mv = self._ledger(1, account["id"])[-1]
        self.assertEqual(mv["type"], "adjust")
        self.assertEqual(mv["actor_user_id"], self.actor_user_id)
        self.assertEqual(mv["reason"], "cortesía")

    def test_saldo_nunca_negativo(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        res = loyalty.adjust_points(1, account["id"], -999, "reversal", actor_user_id=self.actor_user_id)
        self.assertFalse(res["success"])
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 2)

    def test_ajuste_requiere_actor(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        apt = self._create_turno()
        self._complete(apt, 1)
        account = self._account(1, "3815000001")
        res = loyalty.adjust_points(1, account["id"], 5, "cortesía", actor_user_id=None)
        self.assertFalse(res["success"])
class TestLoyaltyMultiTenant(LoyaltyBase):

    def test_business_a_no_ve_cuenta_de_b(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        database.update_loyalty_settings_scoped(2, enabled=True, points_per_completed_appointment=2)
        apt_a = self._create_turno(business_id=1, phone="3815111111")
        self._complete(apt_a, 1)
        self.assertIsNone(self._account(2, "3815111111"))

    def test_appointment_de_b_no_acredita_en_a(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        database.update_loyalty_settings_scoped(2, enabled=True, points_per_completed_appointment=2)
        apt_b = self._create_turno(business_id=2, phone="3815222222")
        self._complete(apt_b, 2)
        self.assertIsNone(self._account(1, "3815222222"))
        self.assertEqual(self._account(2, "3815222222")["points_balance"], 2)

    def test_no_se_puede_modificar_cuenta_de_b_desde_a(self):
        database.update_loyalty_settings_scoped(2, enabled=True, points_per_completed_appointment=2)
        apt_b = self._create_turno(business_id=2, phone="3815333333")
        self._complete(apt_b, 2)
        account_b = self._account(2, "3815333333")
        self.assertIsNone(database.get_loyalty_account_by_id_scoped(account_b["id"], 1))
        res = loyalty.adjust_points(1, account_b["id"], 10, "intento", actor_user_id=self.actor_user_id)
        self.assertFalse(res["success"])

    def test_mismo_phone_saldos_independientes_por_tenant(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=2)
        database.update_loyalty_settings_scoped(2, enabled=True, points_per_completed_appointment=2)
        apt_a = self._create_turno(business_id=1, phone="3815444444")
        self._complete(apt_a, 1)
        apt_b = self._create_turno(business_id=2, phone="3815444444")
        self._complete(apt_b, 2)
        self.assertEqual(self._account(1, "3815444444")["points_balance"], 2)
        self.assertEqual(self._account(2, "3815444444")["points_balance"], 2)


class TestLoyaltyEstados(LoyaltyBase):

    def test_completed_y_luego_cancelled_sticky(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=4)
        apt = self._create_turno()
        self._complete(apt, 1)
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 4)
        database.update_appointment_status_scoped(apt, "cancelled", 1)
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 4)

    def test_completed_reprocesado_no_duplica(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=1)
        apt = self._create_turno()
        self._complete(apt, 1)
        database.update_appointment_status_scoped(apt, "confirmed", 1)
        database.update_appointment_status_scoped(apt, "completed", 1)
        res = loyalty.award_points_for_completed(1, apt)
        self.assertEqual(res["reason"], "already_awarded")
        self.assertEqual(self._account(1, "3815000001")["points_balance"], 1)


class TestLoyaltyNormalizacion(LoyaltyBase):

    def test_telefonos_con_distintos_formatos_misma_cuenta(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=1)
        apt1 = self._create_turno(phone="3815-000001")
        self._complete(apt1, 1)
        apt2 = _insert_confirmed_appointment(1, self.valid_date, "12:00", phone="(3815) 0000-01")
        self._complete(apt2, 1)
        account = self._account(1, "3815000001")
        self.assertIsNotNone(account)
        self.assertEqual(account["points_balance"], 2)


class TestLoyaltyAdminPanelHTTP(LoyaltyBase):

    def test_pagina_fidelizacion_requiere_login(self):
        c = application.app.test_client()
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=1)
        response = c.get("/admin/fidelizacion")
        self.assertEqual(response.status_code, 302)

    def test_admin_puede_configurar(self):
        response = self.client.post(
            "/admin/fidelizacion/configuracion",
            data={
                "csrf_token": self.csrf_token,
                "enabled": "1",
                "points_per_completed_appointment": "2",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(database.get_loyalty_settings_scoped(1)["enabled"], 1)
        self.assertEqual(database.get_loyalty_settings_scoped(1)["points_per_completed_appointment"], 2)

    def test_pagina_muestra_clientes_con_saldo(self):
        database.update_loyalty_settings_scoped(1, enabled=True, points_per_completed_appointment=1)
        apt = self._create_turno(name="Ana", phone="3815777777")
        self._complete(apt, 1)
        response = self.client.get("/admin/fidelizacion")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ana".encode(), response.data)
        self.assertIn(b"Fidelizaci\xc3\xb3n", response.data)


if __name__ == "__main__":
    unittest.main()