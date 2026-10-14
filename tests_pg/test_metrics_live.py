"""Tests live del panel de métricas contra PostgreSQL real.

Valida que las agregaciones de `services.metrics` (GROUP BY, JOINs con
`business_id`, DATE/TIME/TIMESTAMPTZ tipados) devuelvan los mismos resultados
que en SQLite, con aislamiento multi-tenant.

Requiere TURNOBOT_PG_URL (suite omitida sin esa variable).
"""

import datetime

import pytest

from services import metrics
from tests_pg._helpers import make_pg_proxy, seed_business

START = datetime.date(2026, 3, 9)
END = datetime.date(2026, 3, 15)
PREV_START = datetime.date(2026, 3, 2)
PREV_END = datetime.date(2026, 3, 8)


def _utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.UTC)


@pytest.fixture
def metrics_conn(pg_pool, pg_seed, monkeypatch):
    """Dirige services.metrics + database.database al pool de la base de test."""
    import database.database as database

    monkeypatch.setattr("services.metrics.get_connection", lambda: make_pg_proxy(pg_pool))
    monkeypatch.setattr("database.database.get_connection", lambda: make_pg_proxy(pg_pool))
    return database


def _appt(url, business_id, service, day, time, status, phone="5491100000"):
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO appointments (customer_name, phone, service, appointment_date,"
            " appointment_time, appointment_end, duration, status, business_id)"
            " VALUES ('Cliente', %s, %s, %s, %s, %s, 30, %s, %s)",
            (
                phone,
                service,
                day,
                datetime.time(int(time[:2]), int(time[3:])),
                datetime.time(int(time[:2]), int(time[3:])),
                status,
                business_id,
            ),
        )


def _seed_operativa(url, business_id):
    _appt(url, business_id, "Corte", datetime.date(2026, 3, 10), "10:00", "completed", "5491111111")
    _appt(url, business_id, "Barba", datetime.date(2026, 2, 20), "10:00", "completed", "5492222222")
    _appt(url, business_id, "Barba", datetime.date(2026, 3, 11), "11:00", "completed", "5492222222")
    _appt(url, business_id, "Corte", datetime.date(2026, 3, 13), "09:00", "confirmed")
    _appt(url, business_id, "Color", datetime.date(2026, 3, 14), "09:00", "cancelled")
    _appt(url, business_id, "Corte", datetime.date(2026, 3, 14), "10:00", "no_show")
    _appt(url, business_id, "Corte", datetime.date(2026, 3, 5), "10:00", "completed", "5495555555")


def _seed_chat(url, business_id):
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO conversation_sessions (business_id, customer_phone, status,"
            " needs_human, created_at, updated_at) VALUES (%s, %s, 'active', FALSE, %s, %s)"
            " RETURNING id",
            (business_id, "5491111111", _utc(2026, 3, 10, 12), _utc(2026, 3, 10, 12)),
        ).fetchone()
        s1 = row[0]
        conn.execute(
            "INSERT INTO conversation_messages (session_id, role, content, needs_human,"
            " created_at) VALUES (%s, 'user', 'hola', FALSE, %s)",
            (s1, _utc(2026, 3, 10, 12, 1)),
        )
        conn.execute(
            "INSERT INTO conversation_messages (session_id, role, content, needs_human,"
            " created_at) VALUES (%s, 'assistant', 'hola', FALSE, %s)",
            (s1, _utc(2026, 3, 10, 12, 2)),
        )
        # Sesión antigua con mensaje reciente (21:30 BA del 03-10).
        row = conn.execute(
            "INSERT INTO conversation_sessions (business_id, customer_phone, status,"
            " needs_human, created_at, updated_at) VALUES (%s, %s, 'active', FALSE, %s, %s)"
            " RETURNING id",
            (business_id, "5492222222", _utc(2026, 2, 1, 10), _utc(2026, 2, 1, 10)),
        ).fetchone()
        s2 = row[0]
        conn.execute(
            "INSERT INTO conversation_messages (session_id, role, content, needs_human,"
            " created_at) VALUES (%s, 'user', 'hola', FALSE, %s)",
            (s2, _utc(2026, 3, 11, 0, 30)),
        )
        # Derivación con resolución (2,5 h).
        row = conn.execute(
            "INSERT INTO conversation_sessions (business_id, customer_phone, status,"
            " needs_human, human_requested_at, resolved_at, created_at, updated_at)"
            " VALUES (%s, %s, 'human_resolved', FALSE, %s, %s, %s, %s) RETURNING id",
            (
                business_id,
                "5493333333",
                _utc(2026, 3, 9, 4),
                _utc(2026, 3, 9, 6, 30),
                _utc(2026, 3, 9, 3),
                _utc(2026, 3, 9, 6, 30),
            ),
        ).fetchone()
        s3 = row[0]
        conn.execute(
            "INSERT INTO conversation_messages (session_id, role, content, needs_human,"
            " created_at) VALUES (%s, 'user', 'hola', FALSE, %s)",
            (s3, _utc(2026, 3, 9, 3, 30)),
        )


def _seed_loyalty(url, business_id):
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO loyalty_accounts (business_id, customer_phone, points_balance)"
            " VALUES (%s, %s, 0) RETURNING id",
            (business_id, "5491111111"),
        ).fetchone()
        account_id = row[0]
        conn.execute(
            "INSERT INTO points_ledger (business_id, account_id, delta, type, reason,"
            " created_at) VALUES (%s, %s, 10, 'earn', 'turno completado', %s)",
            (business_id, account_id, _utc(2026, 3, 10, 15)),
        )
        row = conn.execute(
            "INSERT INTO rewards (business_id, name, points_cost, active)"
            " VALUES (%s, 'Café', 20, TRUE) RETURNING id",
            (business_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO redemptions (business_id, account_id, reward_id, points_used,"
            " status, idempotency_key, created_at) VALUES (%s, %s, %s, 20, 'redeemed', 'k1', %s)",
            (business_id, account_id, row[0], _utc(2026, 3, 11, 10)),
        )


@pytest.mark.pg_live
def test_pg_metricas_resumen_y_servicios(pg_pool, pg_seed, metrics_conn):
    """Conteos por estado, tasas, ranking y comparación contra PG real."""
    _seed_operativa(pg_seed["url"], pg_seed["business_id"])
    data = metrics.get_metrics(pg_seed["business_id"], START, END, PREV_START, PREV_END)
    summary = data["summary"]
    assert summary["total"] == 5
    assert summary["by_status"]["completed"] == 2
    assert summary["by_status"]["confirmed"] == 1
    assert summary["by_status"]["cancelled"] == 1
    assert summary["by_status"]["no_show"] == 1
    assert summary["cancel_rate"] == round(1 / 5 * 100, 1)
    assert summary["absence_rate"] == round(1 / 3 * 100, 1)
    assert data["variations"]["total"] == {"diff": 4, "pct": 400.0}

    ranking = [(item["service"], item["count"]) for item in data["services"]]
    assert ranking[0] == ("Corte", 2)
    assert ("Barba", 1) in ranking
    assert "Color" not in [item["service"] for item in data["services"]]

    clients = data["clients"]
    assert clients["unique_completed"] == 2
    assert clients["new"] == 1
    assert clients["recurrent"] == 1


@pytest.mark.pg_live
def test_pg_metricas_chat_y_loyalty(pg_pool, pg_seed, metrics_conn):
    """Chat por actividad real y fidelización del período en PG."""
    _seed_chat(pg_seed["url"], pg_seed["business_id"])
    _seed_loyalty(pg_seed["url"], pg_seed["business_id"])
    data = metrics.get_metrics(pg_seed["business_id"], START, END, PREV_START, PREV_END)
    chat = data["chat"]
    assert chat["new_sessions"] == 2
    assert chat["active_sessions"] == 3
    assert chat["messages_total"] == 4
    assert chat["messages_by_role"] == {"user": 3, "assistant": 1}
    assert chat["handoffs"] == 1
    assert chat["human_resolved"] == 1
    assert chat["avg_resolution_seconds"] == 9000.0

    loyalty = data["loyalty"]
    assert loyalty["points_earned"] == 10
    assert loyalty["earn_events"] == 1
    assert loyalty["redemptions"] == 1
    assert loyalty["points_redeemed"] == 20
    assert loyalty["accounts"] == 1


@pytest.mark.pg_live
def test_pg_metricas_aislamiento_entre_negocios(pg_pool, pg_seed, metrics_conn):
    """Los datos de otro negocio no se mezclan."""
    _seed_operativa(pg_seed["url"], pg_seed["business_id"])
    otro = seed_business(pg_seed["url"])
    _appt(pg_seed["url"], otro, "Corte", datetime.date(2026, 3, 10), "10:00", "completed")

    data = metrics.get_metrics(pg_seed["business_id"], START, END, PREV_START, PREV_END)
    assert data["summary"]["total"] == 5

    data_otro = metrics.get_metrics(otro, START, END, PREV_START, PREV_END)
    assert data_otro["summary"]["total"] == 1
    assert data_otro["chat"]["active_sessions"] == 0
