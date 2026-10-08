"""Tests de los detectores de recovery (solo lectura).

Cubre las funciones de `database/database.py`:

- `list_stuck_notifications_scoped` (claims `processing` atascados);
- `list_completed_appointments_without_loyalty_scoped` (earns faltantes);
- `list_approved_businesses_without_invitations` (owners sin invitación).

Son consultas read-only sobre SQLite temporal (patrón de
tests/test_weekly_schedules.py): ninguna operación contra PostgreSQL y
ningún envío real. No integran consumidores: validan que los detectores
devuelven exactamente lo que dicen sus docstrings.
"""

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

import database.database as database


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests SQLite puros: no se necesita PostgreSQL, por lo que no se aplica
    el skip funcional del conftest (omite la suite cuando TURNOBOT_PG_URL no
    está definida)."""
    yield


def _iso(minutes_ago=0):
    return (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class RecoveryListsBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.root_dir / "appointments.db"

        self._original_db_backend = os.environ.get("DB_BACKEND")
        self._original_db_url = os.environ.get("DATABASE_URL")
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)

        database.init_database()
        self._open_connections = []

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        if self._original_db_backend is not None:
            os.environ["DB_BACKEND"] = self._original_db_backend
        else:
            os.environ.pop("DB_BACKEND", None)
        if self._original_db_url is not None:
            os.environ["DATABASE_URL"] = self._original_db_url
        else:
            os.environ.pop("DATABASE_URL", None)
        for conn in self._open_connections:
            conn.close()
        self.temp_dir.cleanup()

    def _conn(self):
        conn = database.get_connection()
        self._open_connections.append(conn)
        return conn

    def _business(self, bid, name, active=1, pending=0):
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO businesses (id, name, slug) VALUES (?, ?, ?)", (bid, name, name)
        )
        conn.execute(
            "UPDATE businesses SET active = ?, pending = ? WHERE id = ?", (active, pending, bid)
        )
        conn.commit()

    def _appointment(self, aid, bid, status="confirmed"):
        # Hora distinta por turno: evita el índice único parcial de slots
        # confirmados (business_id, appointment_date, appointment_time).
        start = f"10:{aid:02d}"
        self._conn().execute(
            "INSERT INTO appointments (id, business_id, customer_name, phone,"
            " service, appointment_date, appointment_time, appointment_end,"
            " duration, status)"
            " VALUES (?, ?, 'Cli', '3815000001', 'Corte', '2026-01-01', ?,"
            " '11:00', 60, ?)",
            (aid, bid, start, status),
        )
        self._open_connections[-1].commit()


class TestStuckNotifications(RecoveryListsBase):
    def _log(self, aid, bid, status, minutes_ago, ntype="confirmation"):
        self._appointment(aid, bid)
        self._conn().execute(
            "INSERT INTO notification_log (appointment_id, business_id, type,"
            " channel, destination, status, error, last_attempt_at)"
            " VALUES (?, ?, ?, 'email', 'c@example.com', ?, '', ?)",
            (aid, bid, ntype, status, _iso(minutes_ago)),
        )
        self._open_connections[-1].commit()

    def test_solo_processing_vencido_es_stuck(self):
        self._business(101, "b1")
        self._log(1, 101, "processing", minutes_ago=60)  # stuck
        self._log(2, 101, "processing", minutes_ago=1)  # en curso
        self._log(3, 101, "failed", minutes_ago=60)  # lo cubre el retry de failed
        self._log(4, 101, "sent", minutes_ago=60)

        stuck = database.list_stuck_notifications_scoped(900, business_id=101)
        self.assertEqual([r["appointment_id"] for r in stuck], [1])
        self.assertEqual(stuck[0]["destination"], "c@example.com")

    def test_umbral_personalizado(self):
        self._business(101, "b1")
        self._log(1, 101, "processing", minutes_ago=30)
        self.assertEqual(database.list_stuck_notifications_scoped(3600, business_id=101), [])
        self.assertEqual(len(database.list_stuck_notifications_scoped(60, business_id=101)), 1)

    def test_scoping_por_negocio(self):
        self._business(101, "b1")
        self._business(102, "b2")
        self._log(1, 101, "processing", minutes_ago=60)
        self._log(2, 102, "processing", minutes_ago=60)

        self.assertEqual(
            [r["appointment_id"] for r in database.list_stuck_notifications_scoped(900)], [1, 2]
        )
        self.assertEqual(
            [
                r["appointment_id"]
                for r in database.list_stuck_notifications_scoped(900, business_id=102)
            ],
            [2],
        )

    def test_sin_filas_devuelve_vacio(self):
        self._business(101, "b1")
        self.assertEqual(database.list_stuck_notifications_scoped(900, business_id=101), [])


class TestMissingLoyaltyEarn(RecoveryListsBase):
    def _earn(self, bid, aid, account_id=1):
        self._conn().execute(
            "INSERT INTO points_ledger (business_id, account_id, delta, type,"
            " reason, appointment_id) VALUES (?, ?, 10, 'earn', 't', ?)",
            (bid, account_id, aid),
        )
        self._open_connections[-1].commit()

    def _account(self, bid):
        conn = self._conn()
        conn.execute(
            "INSERT INTO loyalty_accounts (business_id, customer_phone) VALUES (?, '3815000001')",
            (bid,),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def test_solo_completed_sin_earn(self):
        self._business(101, "b1")
        self._appointment(1, 101, status="completed")  # sin earn -> listado
        self._appointment(2, 101, status="completed")  # con earn -> excluido
        self._appointment(3, 101, status="confirmed")  # no completado -> excluido
        self._appointment(4, 101, status="cancelled")  # cancelado -> excluido
        account_id = self._account(101)
        self._earn(101, 2, account_id)

        missing = database.list_completed_appointments_without_loyalty_scoped(101)
        self.assertEqual([r["id"] for r in missing], [1])

    def test_scoping_por_negocio(self):
        self._business(101, "b1")
        self._business(102, "b2")
        self._appointment(1, 101, status="completed")
        self._appointment(2, 102, status="completed")

        self.assertEqual(
            [r["id"] for r in database.list_completed_appointments_without_loyalty_scoped()], [1, 2]
        )
        self.assertEqual(
            [r["id"] for r in database.list_completed_appointments_without_loyalty_scoped(102)], [2]
        )

    def test_reparacion_via_award_es_idempotente(self):
        """El par detector+award es seguro: re-ejecutar award no duplica."""
        self._business(101, "b1")
        self._appointment(1, 101, status="completed")
        self._conn().execute(
            "INSERT INTO loyalty_settings (business_id, enabled,"
            " points_per_completed_appointment) VALUES (101, 1, 10)"
        )
        self._open_connections[-1].commit()

        from services import loyalty

        first = loyalty.award_points_for_completed(101, 1)
        second = loyalty.award_points_for_completed(101, 1)
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(database.list_completed_appointments_without_loyalty_scoped(101), [])


class TestApprovedWithoutInvitations(RecoveryListsBase):
    def _owner(self, bid, email, with_invitation=True, used=False):
        conn = self._conn()
        conn.execute("INSERT INTO users (email, password_hash) VALUES (?, 'x')", (email,))
        user_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO business_users (user_id, business_id, role_id)"
            " VALUES (?, ?, (SELECT id FROM roles WHERE name = 'owner'))",
            (user_id, bid),
        )
        if with_invitation:
            conn.execute(
                "INSERT INTO invitations (business_id, user_id, role_name, email,"
                " token_hash, expires_at, used_at)"
                " VALUES (?, ?, 'owner', ?, 'h', '2099-01-01 00:00:00', ?)",
                (bid, user_id, email, "2026-01-01 00:00:00" if used else None),
            )
        conn.commit()
        return user_id

    def test_aprobado_sin_invitacion_se_reporta(self):
        self._business(101, "b1", active=1, pending=0)
        self._owner(101, "owner@example.com", with_invitation=False)

        flagged = database.list_approved_businesses_without_invitations()
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["id"], 101)
        self.assertEqual(flagged[0]["owner_email"], "owner@example.com")

    def test_con_invitacion_no_se_reporta_aunque_este_usada(self):
        """Aceptar consume (used_at) pero no borra: no es falso positivo."""
        self._business(101, "b1", active=1, pending=0)
        self._owner(101, "owner@example.com", with_invitation=True, used=True)
        self.assertEqual(database.list_approved_businesses_without_invitations(), [])

    def test_pendiente_no_se_reporta(self):
        self._business(101, "b1", active=0, pending=1)
        self._owner(101, "owner@example.com", with_invitation=False)
        self.assertEqual(database.list_approved_businesses_without_invitations(), [])

    def test_sin_miembro_owner_no_se_reporta(self):
        """Sin fila owner en business_users no hay a quién re-invitar:
        el detector es conservador (puede omitir, no inventa)."""
        self._business(101, "b1", active=1, pending=0)
        self.assertEqual(database.list_approved_businesses_without_invitations(), [])

    def test_reparacion_via_resend_deja_de_reportar(self):
        """El par detector+resend es el camino de reparación existente."""
        self._business(101, "b1", active=1, pending=0)
        user_id = self._owner(101, "owner@example.com", with_invitation=False)
        self.assertEqual(len(database.list_approved_businesses_without_invitations()), 1)

        with mock.patch("services.platform.secrets.token_urlsafe", return_value="tok123"):
            from services import platform as platform_service

            platform_service.resend_invitation(101, user_id, "owner@example.com")
        self.assertEqual(database.list_approved_businesses_without_invitations(), [])

    def test_limite_por_defecto(self):
        for i in range(3):
            bid = 110 + i
            self._business(bid, f"b{bid}", active=1, pending=0)
            self._owner(bid, f"o{i}@example.com", with_invitation=False)
        self.assertEqual(len(database.list_approved_businesses_without_invitations(limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
