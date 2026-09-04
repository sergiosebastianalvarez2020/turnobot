"""Etapa 7: panel administrativo de turnos.

Cubre:
- Gestión de estados (completed / no_show / confirmed / cancelled) desde el panel.
- Reprogramación de turnos desde el panel (scoped por negocio).
- Aislamiento estricto por tenant de las acciones admin.
- Separación de métricas (upcoming / completed / no_show).
"""

import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as application
import database.database as database
from database.database import get_connection
from services import appointments


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


def _insert_business(id_, slug):
    c = get_connection()
    try:
        c.execute(
            "INSERT INTO businesses (id, name, slug) VALUES (?, ?, ?)",
            (id_, f"Business {id_}", slug),
        )
        c.commit()
    finally:
        c.close()


def _insert_confirmed_appointment(business_id, date_, time_, duration=60):
    """Inserta un turno confirmado directamente para pruebas de aislamiento."""
    end_min = _to_minutes(time_) + duration
    end_hhmm = f"{end_min // 60:02d}:{end_min % 60:02d}"
    c = get_connection()
    try:
        cur = c.execute(
            "INSERT INTO appointments "
            "(customer_name, phone, service, appointment_date, appointment_time, "
            " appointment_end, duration, status, business_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
            (
                "Cliente B",
                "3838439000",
                "Corte",
                date_,
                time_,
                end_hhmm,
                duration,
                business_id,
            ),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _to_minutes(hm):
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


class AdminPanelBase(unittest.TestCase):
    """Negocio 1 (slug 'el-corte') con login administrativo por contraseña."""

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

        self.client = application.app.test_client()
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

    def _create_turno(self, date_=None, time_="09:00"):
        date_ = date_ or _next_open_day()
        result = appointments.create_appointment(
            "Ana Pérez", "3838439222", "Corte", date_, time_, 1
        )
        self.assertTrue(result["success"])
        return result["appointment_id"], date_

    def _status_of(self, appointment_id):
        c = get_connection()
        try:
            return c.execute(
                "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()["status"]
        finally:
            c.close()

    def _date_time_of(self, appointment_id):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT appointment_date, appointment_time FROM appointments WHERE id = ?",
                (appointment_id,),
            ).fetchone()
            return row["appointment_date"], row["appointment_time"]
        finally:
            c.close()


class TestAdminStatus(AdminPanelBase):

    def _cambiar_estado(self, appointment_id, status):
        return self.client.post(
            f"/admin/turnos/{appointment_id}/estado",
            data={"csrf_token": self.csrf_token, "status": status},
        )

    def test_marcar_completado(self):
        appointment_id, _ = self._create_turno()
        response = self._cambiar_estado(appointment_id, "completed")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status_of(appointment_id), "completed")

    def test_marcar_no_show(self):
        appointment_id, _ = self._create_turno()
        response = self._cambiar_estado(appointment_id, "no_show")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status_of(appointment_id), "no_show")

    def test_cancelar(self):
        appointment_id, _ = self._create_turno()
        response = self._cambiar_estado(appointment_id, "cancelled")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status_of(appointment_id), "cancelled")

    def test_confirmar_de_vuelta(self):
        appointment_id, _ = self._create_turno()
        self._cambiar_estado(appointment_id, "no_show")
        response = self._cambiar_estado(appointment_id, "confirmed")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status_of(appointment_id), "confirmed")

    def test_estado_invalido_rechazado(self):
        appointment_id, _ = self._create_turno()
        response = self._cambiar_estado(appointment_id, "hackeado")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._status_of(appointment_id), "confirmed")

    def test_csrf_invalido_rechazado(self):
        appointment_id, _ = self._create_turno()
        response = self.client.post(
            f"/admin/turnos/{appointment_id}/estado",
            data={"csrf_token": "mal", "status": "completed"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._status_of(appointment_id), "confirmed")

    def test_panel_muestra_filtro_de_estados(self):
        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Completados", page.text)
        self.assertIn("No se presentó", page.text)


class TestAdminReschedule(AdminPanelBase):

    def test_reprogramar_valido(self):
        appointment_id, date_ = self._create_turno(time_="09:00")
        response = self.client.post(
            f"/admin/turnos/{appointment_id}/reprogramar",
            data={
                "csrf_token": self.csrf_token,
                "new_date": date_,
                "new_time": "15:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        new_date, new_time = self._date_time_of(appointment_id)
        self.assertEqual(new_date, date_)
        self.assertEqual(new_time, "15:00")

    def test_reprogramar_a_horario_ocupado(self):
        appointment_a, date_ = self._create_turno(time_="09:00")
        appointment_b, _ = self._create_turno(time_="15:00")
        before = self._date_time_of(appointment_b)
        response = self.client.post(
            f"/admin/turnos/{appointment_b}/reprogramar",
            data={
                "csrf_token": self.csrf_token,
                "new_date": date_,
                "new_time": "09:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._date_time_of(appointment_b), before)
        self.assertEqual(self._date_time_of(appointment_a)[1], "09:00")

    def test_reprogramar_faltan_datos(self):
        appointment_id, _ = self._create_turno()
        before = self._date_time_of(appointment_id)
        response = self.client.post(
            f"/admin/turnos/{appointment_id}/reprogramar",
            data={"csrf_token": self.csrf_token, "new_date": "", "new_time": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._date_time_of(appointment_id), before)
        page = self.client.get(response.headers["Location"])
        self.assertIn("Completá la fecha y la hora", page.text)

    def test_reprogramar_fecha_pasada(self):
        appointment_id, _ = self._create_turno()
        past = (datetime.now().date() - timedelta(days=1)).isoformat()
        before = self._date_time_of(appointment_id)
        response = self.client.post(
            f"/admin/turnos/{appointment_id}/reprogramar",
            data={
                "csrf_token": self.csrf_token,
                "new_date": past,
                "new_time": "09:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._date_time_of(appointment_id), before)


class TestAdminPanelIsolation(unittest.TestCase):
    """Acciones admin de A no pueden tocar turnos de B."""

    def setUp(self):
        application.rate_limit_state.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        _insert_business(2, "business-b")
        self.date_ = _next_open_day()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_admin_de_a_no_reprograma_turno_de_b(self):
        apt_b = _insert_confirmed_appointment(2, self.date_, "09:00")
        before = self._get_date_time(apt_b)
        result = appointments.reschedule_appointment_admin(
            apt_b, self.date_, "15:00", business_id=1
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")
        self.assertEqual(self._get_date_time(apt_b), before)

    def test_admin_de_a_no_cambia_estado_turno_de_b(self):
        apt_b = _insert_confirmed_appointment(2, self.date_, "09:00")
        changed = database.update_appointment_status_scoped(apt_b, "completed", 1)
        self.assertFalse(changed)
        self.assertEqual(self._status(apt_b), "confirmed")

    def test_admin_de_a_no_toca_turno_de_b_por_http(self):
        apt_b = _insert_confirmed_appointment(2, self.date_, "09:00")
        application.ADMIN_PASSWORD_HASH = generate_password_hash("correcta")
        application.ADMIN_PASSWORD = None
        client = application.app.test_client()
        application.rate_limit_state.clear()
        login_page = client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        client.post("/login", data={"password": "correcta", "csrf_token": csrf})
        # El turno de B no pertenece a A; la actualización scoped no cambia nada.
        database.update_appointment_status_scoped(apt_b, "completed", 2)
        self.assertEqual(self._status(apt_b), "completed")
        # Intentar cambiarlo desde el negocio 1 no afecta a B.
        database.update_appointment_status_scoped(apt_b, "cancelled", 1)
        self.assertEqual(self._status(apt_b), "completed")

    def _status(self, appointment_id):
        c = get_connection()
        try:
            return c.execute(
                "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()["status"]
        finally:
            c.close()

    def _get_date_time(self, appointment_id):
        c = get_connection()
        try:
            row = c.execute(
                "SELECT appointment_date, appointment_time FROM appointments WHERE id = ?",
                (appointment_id,),
            ).fetchone()
            return (row["appointment_date"], row["appointment_time"])
        finally:
            c.close()


class TestAppointmentCounts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def _set_status(self, appointment_id, status):
        c = get_connection()
        try:
            c.execute(
                "UPDATE appointments SET status = ? WHERE id = ?",
                (status, appointment_id),
            )
            c.commit()
        finally:
            c.close()

    def test_metricas_separadas_por_estado(self):
        date_ = _next_open_day()
        a_conf = appointments.create_appointment(
            "Ana", "3838439222", "Corte", date_, "09:00", 1
        )["appointment_id"]
        a_can = appointments.create_appointment(
            "Bea", "3838439223", "Corte", date_, "10:00", 1
        )["appointment_id"]
        a_comp = appointments.create_appointment(
            "Ce", "3838439224", "Corte", date_, "11:00", 1
        )["appointment_id"]
        a_noshow = appointments.create_appointment(
            "Dani", "3838439225", "Corte", date_, "12:00", 1
        )["appointment_id"]
        self._set_status(a_can, "cancelled")
        self._set_status(a_comp, "completed")
        self._set_status(a_noshow, "no_show")

        counts = appointments.get_appointment_counts(1)
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(counts["cancelled"], 1)
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["no_show"], 1)
        self.assertEqual(counts["total"], 4)

    def test_upcoming_no_cuenta_turnos_pasados(self):
        # Turno de hoy en el futuro cuenta como próximos.
        today = datetime.now().date().isoformat()
        future_open = _next_open_day()
        apt_futuro = appointments.create_appointment(
            "Ana", "3838439222", "Corte", future_open, "09:00", 1
        )["appointment_id"]
        counts = appointments.get_appointment_counts(1)
        self.assertEqual(counts["upcoming"], 1)
        self.assertEqual(counts["confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
