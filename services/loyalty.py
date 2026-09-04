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