"""Panel de métricas: cálculos, filtros de fecha, seguridad y aislamiento.

Cubre (SQLite, base temporal por test):
- Conteo por estado, total (incluye estados históricos desconocidos) y tasas.
- Período sin turnos / división por cero (None, nunca NaN).
- Comparación con período anterior de igual duración, sin solapamiento.
- Rango de un único día.
- Servicios más reservados (confirmed + completed, sin inferir origen).
- Demanda por día y horario (demanda efectiva, no ocupación).
- Clientes nuevos / recurrentes (teléfono normalizado, vacíos excluidos).
- Chat: sesiones nuevas vs. antiguas con mensajes recientes, roles,
  derivaciones, resolución y límites de medianoche en zona del negocio.
- Fidelización: puntos otorgados y canjes del período.
- Filtros: hoy, 7/30 días, mes, rango válido, fecha inválida, invertidas,
  rango > 365 días.
- Seguridad: anónimo, staff, customer, owner, admin, aislamiento entre
  negocios y slug ajeno.
"""

import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection
from services import metrics

START = date(2026, 3, 9)
END = date(2026, 3, 15)
PREV_START = date(2026, 3, 2)
PREV_END = date(2026, 3, 8)


def _exec(sql, params=()):
    connection = get_connection()
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _insert_appointment(
    business_id, service, date_, time_, status="confirmed", phone="5491100000", name="Cliente"
):
    end_min = int(time_[:2]) * 60 + int(time_[3:]) + 30
    end_hhmm = f"{end_min // 60:02d}:{end_min % 60:02d}"
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date,"
            " appointment_time, appointment_end, duration, status, business_id)"
            " VALUES (?, ?, ?, ?, ?, ?, 30, ?, ?)",
            (name, phone, service, date_, time_, end_hhmm, status, business_id),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def _insert_session(business_id, phone, created_at, status="active"):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO conversation_sessions (business_id, customer_phone, status,"
            " needs_human, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (business_id, phone, status, created_at, created_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def _insert_message(session_id, role, created_at):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO conversation_messages (session_id, role, content, needs_human,"
            " created_at) VALUES (?, ?, 'hola', 0, ?)",
            (session_id, role, created_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def _insert_earn(business_id, phone, delta, created_at):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO loyalty_accounts (business_id, customer_phone, points_balance)"
            " VALUES (?, ?, 0)",
            (business_id, phone),
        )
        account_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO points_ledger (business_id, account_id, delta, type, reason,"
            " created_at) VALUES (?, ?, ?, 'earn', 'turno completado', ?)",
            (business_id, account_id, delta, created_at),
        )
        connection.commit()
        return account_id
    finally:
        connection.close()


class MetricsBase(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"

        self._original_db_backend = os.environ.get("DB_BACKEND")
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)

        database.init_database()

        connection = get_connection()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO businesses (id, name, slug) VALUES (1, 'El Corte', 'el-corte')"
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

        self.original_hash = application.ADMIN_PASSWORD_HASH
        self.original_password = application.ADMIN_PASSWORD
        application.ADMIN_PASSWORD_HASH = None
        application.ADMIN_PASSWORD = None

        login_page = self.client.get("/login")
        csrf_login = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        self.client.post(
            "/login",
            data={
                "email": self._owner_email,
                "password": self._owner_password,
                "csrf_token": csrf_login,
            },
        )

    def tearDown(self):
        application.ADMIN_PASSWORD_HASH = self.original_hash
        application.ADMIN_PASSWORD = self.original_password
        database.DATABASE_PATH = self.original_database_path
        if self._original_db_backend is not None:
            os.environ["DB_BACKEND"] = self._original_db_backend
        else:
            os.environ.pop("DB_BACKEND", None)
        self.temp_dir.cleanup()

    def _login_as(self, email, password):
        client = self.app.test_client()
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post("/login", data={"email": email, "password": password, "csrf_token": token})
        return client

    def _make_user(self, email, password, business_id, role):
        user_id = database.create_user_scoped(email, generate_password_hash(password), active=True)
        self.assertIsNotNone(user_id)
        database.create_membership_scoped(user_id, business_id, role)
        return user_id


# ============================================================
# Períodos y validación de filtros
# ============================================================


class TestResolvePeriod(unittest.TestCase):
    TODAY = date(2026, 3, 15)

    def test_today(self):
        resolved = metrics.resolve_period("today", today=self.TODAY)
        self.assertEqual((resolved["start"], resolved["end"]), (self.TODAY, self.TODAY))
        self.assertEqual(resolved["days"], 1)
        self.assertEqual(
            (resolved["prev_start"], resolved["prev_end"]), (date(2026, 3, 14), date(2026, 3, 14))
        )

    def test_last7(self):
        resolved = metrics.resolve_period("last7", today=self.TODAY)
        self.assertEqual(resolved["start"], date(2026, 3, 9))
        self.assertEqual(resolved["days"], 7)
        self.assertEqual((resolved["prev_start"], resolved["prev_end"]), (PREV_START, PREV_END))

    def test_last30(self):
        resolved = metrics.resolve_period("last30", today=self.TODAY)
        self.assertEqual(resolved["start"], date(2026, 2, 14))
        self.assertEqual(resolved["days"], 30)
        self.assertEqual(
            (resolved["prev_start"], resolved["prev_end"]), (date(2026, 1, 15), date(2026, 2, 13))
        )

    def test_month(self):
        resolved = metrics.resolve_period("month", today=self.TODAY)
        self.assertEqual((resolved["start"], resolved["end"]), (date(2026, 3, 1), self.TODAY))
        self.assertEqual(resolved["days"], 15)

    def test_custom_valido_un_dia(self):
        resolved = metrics.resolve_period("custom", "2026-03-10", "2026-03-10", today=self.TODAY)
        self.assertIsNone(resolved["error"])
        self.assertEqual(resolved["days"], 1)
        self.assertEqual(
            (resolved["prev_start"], resolved["prev_end"]), (date(2026, 3, 9), date(2026, 3, 9))
        )

    def test_custom_365_dias_es_valido(self):
        resolved = metrics.resolve_period("custom", "2025-03-16", "2026-03-15", today=self.TODAY)
        self.assertIsNone(resolved["error"])
        self.assertEqual(resolved["days"], 365)

    def test_periodo_desconocido(self):
        self.assertIsNotNone(metrics.resolve_period("trimestre", today=self.TODAY)["error"])

    def test_fecha_invalida(self):
        self.assertIsNotNone(
            metrics.resolve_period("custom", "2026-13-01", "2026-03-10", today=self.TODAY)["error"]
        )
        self.assertIsNotNone(
            metrics.resolve_period("custom", "10/03/2026", "2026-03-10", today=self.TODAY)["error"]
        )
        self.assertIsNotNone(metrics.resolve_period("custom", "", "", today=self.TODAY)["error"])

    def test_fechas_invertidas(self):
        resolved = metrics.resolve_period("custom", "2026-03-10", "2026-03-09", today=self.TODAY)
        self.assertIn("posterior", resolved["error"])

    def test_rango_mayor_a_365(self):
        resolved = metrics.resolve_period("custom", "2025-03-15", "2026-03-15", today=self.TODAY)
        self.assertIn("365", resolved["error"])


# ============================================================
# Cálculos del servicio
# ============================================================


def _seed_operativa(business_id=1):
    # Completados del período.
    _insert_appointment(business_id, "Corte", "2026-03-10", "10:00", "completed", "5491111111")
    _insert_appointment(business_id, "Barba", "2026-02-20", "10:00", "completed", "5492222222")
    _insert_appointment(business_id, "Barba", "2026-03-11", "11:00", "completed", "5492222222")
    _insert_appointment(business_id, "Corte", "2026-03-12", "10:00", "completed", "549 3333333")
    _insert_appointment(business_id, "Corte", "2026-03-12", "10:30", "completed", "5493333333")
    _insert_appointment(business_id, "Corte", "2026-03-12", "12:00", "completed", "")
    _insert_appointment(business_id, "Corte", "2026-03-13", "09:00", "confirmed", "5496666666")
    _insert_appointment(business_id, "Barba", "2026-03-13", "09:30", "confirmed", "5497777777")
    _insert_appointment(business_id, "Color", "2026-03-14", "09:00", "cancelled", "5498888888")
    _insert_appointment(business_id, "Corte", "2026-03-14", "10:00", "no_show", "5499999999")
    _insert_appointment(business_id, "  ", "2026-03-14", "11:00", "completed", "5491010101")
    # Estado histórico desconocido: cuenta en el total, no clasifica.
    _insert_appointment(business_id, "Corte", "2026-03-14", "12:00", "reprogramado", "5491212121")
    # Período anterior.
    _insert_appointment(business_id, "Corte", "2026-03-05", "10:00", "completed", "5495555555")
    _insert_appointment(business_id, "Corte", "2026-03-06", "10:00", "cancelled", "5495656565")
    # Inactivo histórico (2 completados viejos, sin actividad reciente).
    _insert_appointment(business_id, "Corte", "2020-05-01", "10:00", "completed", "5494444444")
    _insert_appointment(business_id, "Corte", "2020-06-01", "10:00", "completed", "5494444444")


def _seed_chat(business_id=1):
    s1 = _insert_session(business_id, "5491111111", "2026-03-10 12:00:00")
    _insert_message(s1, "user", "2026-03-10 12:01:00")
    _insert_message(s1, "assistant", "2026-03-10 12:02:00")
    _insert_message(s1, "user", "2026-03-20 12:00:00")  # fuera del período
    # Sesión antigua con actividad reciente (21:30 hora BA del 03-10).
    s2 = _insert_session(business_id, "5492222222", "2026-02-01 10:00:00")
    _insert_message(s2, "user", "2026-03-11 00:30:00")
    # Límite inferior inclusivo: 00:00 BA del 03-09 == 03:00 UTC.
    s3 = _insert_session(business_id, "5493333333", "2026-03-09 03:00:00")
    _insert_message(s3, "user", "2026-03-09 03:30:00")
    _exec(
        "UPDATE conversation_sessions SET human_requested_at = ?, resolved_at = ?"
        " WHERE id = ? AND business_id = ?",
        ("2026-03-09 04:00:00", "2026-03-09 06:30:00", s3, business_id),
    )
    # Solo actividad previa al período (23:59:59 BA del 03-08): no activa.
    s4 = _insert_session(business_id, "5494444444", "2026-02-15 10:00:00")
    _insert_message(s4, "user", "2026-03-09 02:59:59")


def _seed_loyalty(business_id=1):
    _insert_earn(business_id, "5491111111", 10, "2026-03-10 15:00:00")
    _insert_earn(business_id, "5492222222", 5, "2026-03-01 10:00:00")  # fuera del período
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO rewards (business_id, name, points_cost, active) VALUES (?, 'Café', 20, 1)",
            (business_id,),
        )
        reward_id = cursor.lastrowid
        account = connection.execute(
            "SELECT id FROM loyalty_accounts WHERE business_id = ? AND customer_phone = ?",
            (business_id, "5491111111"),
        ).fetchone()
        connection.execute(
            "INSERT INTO redemptions (business_id, account_id, reward_id, points_used,"
            " status, idempotency_key, created_at) VALUES (?, ?, ?, 20, 'redeemed', 'k1', ?)",
            (business_id, account["id"], reward_id, "2026-03-11 10:00:00"),
        )
        connection.execute(
            "INSERT INTO redemptions (business_id, account_id, reward_id, points_used,"
            " status, idempotency_key, created_at) VALUES (?, ?, ?, 20, 'cancelled', 'k2', ?)",
            (business_id, account["id"], reward_id, "2026-03-11 11:00:00"),
        )
        connection.commit()
    finally:
        connection.close()
    _exec(
        "INSERT INTO retention_actions (business_id, customer_phone, action_type,"
        " created_at) VALUES (?, ?, 'viewed', ?)",
        (business_id, "5494444444", "2026-03-12 10:00:00"),
    )


class TestServiceSummary(MetricsBase):
    def test_resumen_tasas_y_comparacion(self):
        _seed_operativa()
        data = metrics.get_metrics(1, START, END, PREV_START, PREV_END)

        summary = data["summary"]
        self.assertEqual(summary["total"], 11)
        self.assertEqual(summary["by_status"]["completed"], 6)
        self.assertEqual(summary["by_status"]["confirmed"], 2)
        self.assertEqual(summary["by_status"]["cancelled"], 1)
        self.assertEqual(summary["by_status"]["no_show"], 1)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["cancel_rate"], 9.1)
        self.assertAlmostEqual(summary["absence_rate"], 14.3, places=1)

        self.assertEqual(data["previous"]["total"], 2)
        self.assertEqual(data["variations"]["total"], {"diff": 9, "pct": 450.0})
        self.assertEqual(data["variations"]["cancelled"], {"diff": 0, "pct": 0.0})

    def test_periodo_vacio_sin_division_por_cero(self):
        data = metrics.get_metrics(1, START, END, PREV_START, PREV_END)
        self.assertEqual(data["summary"]["total"], 0)
        self.assertIsNone(data["summary"]["cancel_rate"])
        self.assertIsNone(data["summary"]["absence_rate"])
        self.assertEqual(data["variations"]["total"], {"diff": 0, "pct": None})
        self.assertEqual(data["services"], [])
        self.assertIsNone(data["demand"]["peak"])

    def test_sin_base_previa_pct_none(self):
        _insert_appointment(1, "Corte", "2026-03-10", "10:00", "completed", "5491111111")
        data = metrics.get_metrics(1, START, END, PREV_START, PREV_END)
        self.assertEqual(data["variations"]["total"], {"diff": 1, "pct": None})


class TestServiceBreakdowns(MetricsBase):
    def test_servicios_demanda_y_clientes(self):
        _seed_operativa()
        data = metrics.get_metrics(1, START, END, PREV_START, PREV_END)

        ranking = [(item["service"], item["count"]) for item in data["services"]]
        self.assertEqual(ranking[0], ("Corte", 5))
        self.assertIn(("Barba", 2), ranking)
        self.assertIn(("(sin especificar)", 1), ranking)
        # El cancelado (Color) no entra al ranking de demanda.
        self.assertNotIn("Color", [item["service"] for item in data["services"]])

        demand = data["demand"]
        self.assertEqual(demand["statuses"], ["confirmed", "completed"])
        total_cells = sum(cell["count"] for cell in demand["cells"])
        self.assertEqual(total_cells, 8)  # 6 completados + 2 confirmados
        self.assertEqual(sum(item["count"] for item in demand["by_weekday"]), total_cells)
        self.assertIsNotNone(demand["peak"])

        clients = data["clients"]
        # p1 nuevo; p2 recurrente; p3 fusionado (2 variantes -> 1) nuevo;
        # p4 sin teléfono excluido; sin-nombre con teléfono cuenta.
        self.assertEqual(clients["unique_completed"], 4)
        self.assertEqual(clients["new"], 3)
        self.assertEqual(clients["recurrent"], 1)
        # p2 y p3 (último completado en marzo, hoy real > 60 días después)
        # también califican como inactivos, además del histórico p5.
        self.assertEqual(clients["inactive_count"], 3)

    def test_rango_un_dia(self):
        _seed_operativa()
        day = date(2026, 3, 12)
        data = metrics.get_metrics(1, day, day, date(2026, 3, 11), date(2026, 3, 11))
        self.assertEqual(data["summary"]["total"], 3)
        self.assertEqual(data["summary"]["by_status"]["completed"], 3)


class TestServiceChatLoyalty(MetricsBase):
    def test_chat_actividad_real_y_medianoche(self):
        _seed_chat()
        data = metrics.get_metrics(1, START, END, PREV_START, PREV_END)["chat"]
        self.assertEqual(data["new_sessions"], 2)
        self.assertEqual(data["active_sessions"], 3)
        self.assertEqual(data["messages_total"], 4)
        self.assertEqual(data["messages_by_role"], {"user": 3, "assistant": 1})
        self.assertEqual(data["handoffs"], 1)
        self.assertEqual(data["human_resolved"], 1)
        self.assertEqual(data["avg_resolution_seconds"], 9000.0)

    def test_chat_sin_datos(self):
        chat = metrics.get_metrics(1, START, END, PREV_START, PREV_END)["chat"]
        self.assertEqual(chat["active_sessions"], 0)
        self.assertIsNone(chat["avg_resolution_seconds"])

    def test_loyalty_periodo(self):
        _seed_loyalty()
        loyalty = metrics.get_metrics(1, START, END, PREV_START, PREV_END)["loyalty"]
        self.assertEqual(loyalty["points_earned"], 10)
        self.assertEqual(loyalty["earn_events"], 1)
        self.assertEqual(loyalty["redemptions"], 1)
        self.assertEqual(loyalty["points_redeemed"], 20)
        self.assertEqual(loyalty["accounts"], 2)
        self.assertEqual(loyalty["retention_actions"], {"viewed": 1})


# ============================================================
# Rutas: filtros, seguridad y aislamiento
# ============================================================


class TestMetricasRoutes(MetricsBase):
    METRICAS = "/admin/metricas?period=custom&start_date=2026-03-09&end_date=2026-03-15"

    def test_owner_ve_panel(self):
        _seed_operativa()
        response = self.client.get(self.METRICAS)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Métricas", response.text)
        self.assertIn("Turnos totales", response.text)
        self.assertIn("Fidelización", response.text)
        self.assertIn("/admin/metricas", response.text)  # enlace en la navegación

    def test_admin_ve_panel(self):
        self._make_user("admin@test.local", "testpass123", 1, "admin")
        client = self._login_as("admin@test.local", "testpass123")
        self.assertEqual(client.get("/admin/metricas").status_code, 200)

    def test_staff_no_accede(self):
        self._make_user("staff@test.local", "testpass123", 1, "staff")
        client = self._login_as("staff@test.local", "testpass123")
        response = client.get("/admin/metricas")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_customer_no_accede(self):
        self._make_user("cli@test.local", "testpass123", 1, "customer")
        client = self._login_as("cli@test.local", "testpass123")
        response = client.get("/admin/metricas")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_anonimo_redirige(self):
        client = self.app.test_client()
        response = client.get("/admin/metricas")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_slug_propio_y_ajeno(self):
        self.assertEqual(self.client.get("/b/el-corte/admin/metricas").status_code, 200)
        self.assertEqual(self.client.get("/b/otro-negocio/admin/metricas").status_code, 404)

    def test_aislamiento_entre_negocios(self):
        _seed_operativa()
        _exec("INSERT INTO businesses (id, name, slug) VALUES (2, 'Otro', 'otro')")
        for hour in ("09:00", "10:00", "11:00"):
            _insert_appointment(2, "Corte", "2026-03-10", hour, "completed", "5492000000")
        response = self.client.get(self.METRICAS)
        # 11 turnos del negocio 1; los 3 del negocio 2 no se mezclan.
        self.assertIn('<div class="stat-value">11</div>', response.text)
        self.assertNotIn("5492000000", response.text)

    def test_filtros_invalidos_muestran_error(self):
        for query in (
            "period=custom&start_date=2026-13-01&end_date=2026-03-10",
            "period=custom&start_date=2026-03-10&end_date=2026-03-09",
            "period=custom&start_date=2025-03-15&end_date=2026-03-15",
            "period=inexistente",
        ):
            response = self.client.get(f"/admin/metricas?{query}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("settings-error", response.text)

    def test_periodos_predefinidos(self):
        _seed_operativa()
        for period in ("today", "last7", "last30", "month"):
            response = self.client.get(f"/admin/metricas?period={period}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Métricas", response.text)


if __name__ == "__main__":
    unittest.main()
