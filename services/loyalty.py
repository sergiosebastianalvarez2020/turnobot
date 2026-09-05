"""Capa de negocio de fidelización por puntos (MVP Etapa 10.1).

Reglas (ver ETAPA10_DISENO_FIDELIZACION.md):
- Solo un turno `completed` puede acreditar puntos.
- Identidad: business_id + phone normalizado (ANCLA ÚNICA).
- Acreditación idempotente por appointment/tenant (UNIQUE parcial + transaccion).
- Adherencia "sticky": un cambio posterior de estado NO revierte.
- Saldo = SUM(delta) del ledger; nunca negativo.
- Todo acceso está scoped por business_id.

Sigue el patrón de services/memberships.py (devuelve dicts success/reason).
"""

import logging
from datetime import date

from services.appointments import normalize_phone
from database.database import (
    get_connection,
    get_loyalty_settings_scoped,
    get_loyalty_account_by_id_scoped,
    recalculate_all_balances_scoped,
    list_points_ledger_scoped,
    list_loyalty_accounts_scoped,
)

logger = logging.getLogger("turnobot.loyalty")


def award_points_for_completed(business_id, appointment_id):
    """Acredita puntos por un turno completado (idempotente).

    success=True también ante omisiones intencionales (fidelización off, ya
    acreditado, config 0, sin phone). success=False solo si el turno no pertenece
    al negocio. Seguro ante reintentos/concurrencia gracias al UNIQUE parcial
    idx_points_ledger_earn_per_appointment bajo BEGIN IMMEDIATE.
    """
    if business_id is None:
        return {"success": False, "reason": "invalid_business"}
    try:
        appointment_id = int(appointment_id)
    except (TypeError, ValueError):
        return {"success": False, "reason": "invalid_appointment"}

    connection = get_connection()
    try:
        appointment = connection.execute(
            """SELECT id, customer_name, phone, customer_email, status, business_id
               FROM appointments WHERE id = ? AND business_id = ?""",
            (appointment_id, business_id),
        ).fetchone()
        if appointment is None:
            return {"success": False, "reason": "appointment_not_found"}
        appointment = dict(appointment)
        if appointment["status"] != "completed":
            return {"success": False, "reason": "not_completed"}

        settings = get_loyalty_settings_scoped(business_id)
        if settings is None or not settings.get("enabled"):
            return {"success": True, "reason": "disabled"}
        points = settings.get("points_per_completed_appointment") or 0
        if points <= 0:
            return {"success": True, "reason": "config_zero"}

        phone = normalize_phone(appointment.get("phone") or "")
        if not phone or len(phone) < 7:
            return {"success": True, "reason": "missing_phone"}

        connection.execute("BEGIN IMMEDIATE")
        try:
            account = connection.execute(
                """SELECT * FROM loyalty_accounts WHERE business_id = ? AND customer_phone = ?""",
                (business_id, phone),
            ).fetchone()
            if account is None:
                email = (appointment.get("customer_email") or "").strip().lower() or None
                name = (appointment.get("customer_name") or "").strip() or None
                cur = connection.execute(
                    """INSERT INTO loyalty_accounts
                       (business_id, customer_phone, customer_email, customer_name, points_balance)
                       VALUES (?, ?, ?, ?, 0)""",
                    (business_id, phone, email, name),
                )
                account = connection.execute(
                    "SELECT * FROM loyalty_accounts WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
            else:
                email = (appointment.get("customer_email") or "").strip().lower() or None
                name = (appointment.get("customer_name") or "").strip() or None
                fields, params = [], []
                if email and (account["customer_email"] or None) != email:
                    fields.append("customer_email = ?"); params.append(email)
                if name and (account["customer_name"] or None) != name:
                    fields.append("customer_name = ?"); params.append(name)
                if fields:
                    params.append(account["id"])
                    connection.execute(
                        "UPDATE loyalty_accounts SET "
                        + ", ".join(fields)
                        + ", updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        params,
                    )

            try:
                connection.execute(
                    """INSERT INTO points_ledger
                       (business_id, account_id, delta, type, reason,
                        appointment_id, points_per_completed)
                       VALUES (?, ?, ?, 'earn', 'turno completado', ?, ?)""",
                    (business_id, account["id"], points, appointment_id, points),
                )
            except Exception:
                connection.execute("ROLLBACK")
                return {"success": True, "reason": "already_awarded"}

            total = connection.execute(
                """SELECT COALESCE(SUM(delta), 0) AS t FROM points_ledger
                   WHERE business_id = ? AND account_id = ?""",
                (business_id, account["id"]),
            ).fetchone()
            connection.execute(
                """UPDATE loyalty_accounts SET points_balance = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND business_id = ?""",
                (total["t"], account["id"], business_id),
            )
            connection.commit()
            return {"success": True, "reason": "awarded", "points": points}
        except Exception:
            connection.execute("ROLLBACK")
            logger.exception("Error acreditando puntos para appointment %s", appointment_id)
            return {"success": False, "reason": "error"}
    finally:
        connection.close()
# ============================================================
# Consultas (scoped)
# ============================================================

def get_settings(business_id):
    """Configuración de fidelización del negocio (o None)."""
    return get_loyalty_settings_scoped(business_id)


def get_account(business_id, customer_phone):
    """Cuenta de un cliente por phone (None si no existe)."""
    from database.database import get_loyalty_account_scoped
    return get_loyalty_account_scoped(business_id, customer_phone)


def get_balance(business_id, customer_phone):
    """Saldo actual de un cliente, o 0 si aún no tiene cuenta."""
    account = get_account(business_id, customer_phone)
    return account["points_balance"] if account else 0


def get_account_movements(business_id, account_id):
    """Movimientos del ledger de una cuenta del negocio."""
    if get_loyalty_account_by_id_scoped(account_id, business_id) is None:
        return []
    return list_points_ledger_scoped(business_id, account_id)


def get_loyalty_clients(business_id):
    """Clientes con cuenta de fidelización del negocio (ordenados por saldo)."""
    return list_loyalty_accounts_scoped(business_id)


def rebalance(business_id):
    """Reconstruye el saldo de todas las cuentas desde el ledger (reconciliación)."""
    return recalculate_all_balances_scoped(business_id)


# ============================================================
# Ajustes administrativos (scoped)
# ============================================================

def adjust_points(business_id, account_id, delta, reason, actor_user_id):
    """Ajuste manual (+/-) de puntos de una cuenta del negocio.

    Requiere motivo y actor (admin). El saldo nunca queda negativo. Devuelve
    `{"success": bool, "reason": str|None}`.
    """
    if business_id is None:
        return {"success": False, "reason": "invalid_business"}
    if actor_user_id is None:
        return {"success": False, "reason": "actor_required"}

    account = get_loyalty_account_by_id_scoped(account_id, business_id)
    if account is None:
        return {"success": False, "reason": "account_not_found"}

    try:
        delta_int = int(delta)
    except (TypeError, ValueError):
        return {"success": False, "reason": "invalid_delta"}
    if delta_int == 0:
        return {"success": False, "reason": "zero_delta"}
    if account["points_balance"] + delta_int < 0:
        return {"success": False, "reason": "negative_balance"}
    reason = (reason or "").strip()
    if not reason:
        return {"success": False, "reason": "reason_required"}

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT INTO points_ledger
                   (business_id, account_id, delta, type, reason, actor_user_id)
                   VALUES (?, ?, ?, 'adjust', ?, ?)""",
                (business_id, account_id, delta_int, reason, actor_user_id),
            )
            total = connection.execute(
                """SELECT COALESCE(SUM(delta), 0) AS t FROM points_ledger
                   WHERE business_id = ? AND account_id = ?""",
                (business_id, account_id),
            ).fetchone()
            connection.execute(
                """UPDATE loyalty_accounts SET points_balance = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND business_id = ?""",
                (total["t"], account_id, business_id),
            )
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return {"success": True, "reason": None}


def list_rewards(business_id, active_only=False):
    connection = get_connection()
    try:
        query = "SELECT * FROM rewards WHERE business_id = ?"
        params = [business_id]
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY active DESC, points_cost ASC, id ASC"
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def save_reward(business_id, reward_id, name, description, points_cost, active=True):
    name = (name or "").strip()
    try:
        points_cost = int(points_cost)
    except (TypeError, ValueError):
        return {"success": False, "reason": "invalid_points"}
    if not name or points_cost <= 0:
        return {"success": False, "reason": "invalid_reward"}
    connection = get_connection()
    try:
        if reward_id:
            cur = connection.execute("UPDATE rewards SET name=?, description=?, points_cost=?, active=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND business_id=?", (name, (description or "").strip() or None, points_cost, int(bool(active)), reward_id, business_id))
        else:
            cur = connection.execute("INSERT INTO rewards (business_id,name,description,points_cost,active) VALUES (?,?,?,?,?)", (business_id, name, (description or "").strip() or None, points_cost, int(bool(active))))
        connection.commit()
        return {"success": cur.rowcount == 1, "reason": None if cur.rowcount == 1 else "not_found"}
    except Exception:
        connection.rollback()
        return {"success": False, "reason": "duplicate_name"}
    finally:
        connection.close()


def redeem(business_id, account_id, reward_id, idempotency_key, actor_user_id=None):
    if not idempotency_key or not get_loyalty_settings_scoped(business_id) or not get_loyalty_settings_scoped(business_id).get("enabled"):
        return {"success": False, "reason": "disabled"}
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM redemptions WHERE business_id=? AND idempotency_key=?", (business_id, idempotency_key)).fetchone()
        if existing:
            connection.commit()
            return {"success": existing["status"] == "redeemed", "reason": "already_processed", "redemption": dict(existing)}
        account = connection.execute("SELECT * FROM loyalty_accounts WHERE id=? AND business_id=?", (account_id, business_id)).fetchone()
        reward = connection.execute("SELECT * FROM rewards WHERE id=? AND business_id=? AND active=1", (reward_id, business_id)).fetchone()
        if not account or not reward:
            connection.rollback(); return {"success": False, "reason": "not_found"}
        balance = connection.execute("SELECT COALESCE(SUM(delta),0) FROM points_ledger WHERE business_id=? AND account_id=?", (business_id, account_id)).fetchone()[0]
        if balance < reward["points_cost"]:
            connection.rollback(); return {"success": False, "reason": "insufficient_points"}
        cur = connection.execute("INSERT INTO redemptions (business_id,account_id,reward_id,points_used,idempotency_key,actor_user_id) VALUES (?,?,?,?,?,?)", (business_id, account_id, reward_id, reward["points_cost"], idempotency_key, actor_user_id))
        redemption_id = cur.lastrowid
        connection.execute("INSERT INTO points_ledger (business_id,account_id,delta,type,reason,actor_user_id,reward_id,redemption_id) VALUES (?,?,?,'redeem',?,?,?,?)", (business_id, account_id, -reward["points_cost"], "recompensa canjeada", actor_user_id, reward_id, redemption_id))
        connection.execute("UPDATE loyalty_accounts SET points_balance=points_balance-?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND business_id=? AND points_balance>=?", (reward["points_cost"], account_id, business_id, reward["points_cost"]))
        connection.commit()
        return {"success": True, "reason": "redeemed", "redemption_id": redemption_id}
    except Exception:
        connection.rollback()
        return {"success": False, "reason": "error"}
    finally:
        connection.close()


def retention_candidates(business_id):
    connection = get_connection()
    try:
        rows = connection.execute("SELECT phone, customer_name, appointment_date FROM appointments WHERE business_id=? AND status='completed' AND phone IS NOT NULL AND phone!=''", (business_id,)).fetchall()
        grouped = {}
        today = date.today()
        for row in rows:
            phone = normalize_phone(row["phone"])
            if not phone:
                continue
            item = grouped.setdefault(phone, {"customer_phone": phone, "customer_name": row["customer_name"], "completed_count": 0, "last_completed_date": row["appointment_date"]})
            item["completed_count"] += 1
            if row["appointment_date"] > item["last_completed_date"]:
                item["last_completed_date"] = row["appointment_date"]
                item["customer_name"] = row["customer_name"]
        result = []
        for item in grouped.values():
            try:
                item["days_since_last"] = (today - date.fromisoformat(item["last_completed_date"])).days
            except ValueError:
                continue
            if item["completed_count"] >= 2 and item["days_since_last"] >= 60:
                result.append(item)
        return sorted(result, key=lambda value: value["days_since_last"], reverse=True)
    finally:
        connection.close()
