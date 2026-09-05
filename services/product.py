"""Resumen comercial y guía inicial, derivados de datos existentes."""

from database.database import get_connection


def get_product_summary(business_id):
    connection = get_connection()
    try:
        counts = {row["status"]: row["total"] for row in connection.execute("SELECT status, COUNT(*) total FROM appointments WHERE business_id=? GROUP BY status", (business_id,)).fetchall()}
        recurring = connection.execute("SELECT COUNT(*) FROM (SELECT phone FROM appointments WHERE business_id=? AND status='completed' AND phone IS NOT NULL GROUP BY phone HAVING COUNT(*) >= 2)", (business_id,)).fetchone()[0]
        points = connection.execute("SELECT COALESCE(SUM(delta),0) FROM points_ledger WHERE business_id=? AND type='earn'", (business_id,)).fetchone()[0]
        rewards = connection.execute("SELECT COUNT(*) FROM redemptions WHERE business_id=? AND status='redeemed'", (business_id,)).fetchone()[0]
        return {"appointments": sum(counts.values()), "completed": counts.get("completed", 0), "cancelled": counts.get("cancelled", 0), "no_show": counts.get("no_show", 0), "recurring": recurring, "points_awarded": points, "rewards_redeemed": rewards}
    finally:
        connection.close()


def get_onboarding_state(business_id, settings, services):
    summary = get_product_summary(business_id)
    settings = dict(settings) if settings is not None else None
    return {"business_configured": bool(settings and settings.get("business_name")), "has_service": bool(services), "has_appointment": summary["appointments"] > 0, "summary": summary}
