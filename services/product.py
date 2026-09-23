"""Resumen comercial y guía inicial, derivados de datos existentes."""

from typing import Any

from database.database import get_connection, get_resources_scoped
from services.knowledge import get_knowledge_scoped
from services.notifications import smtp_configured


def get_product_summary(business_id: int) -> dict[str, int]:
    connection = get_connection()
    try:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute(
                "SELECT status, COUNT(*) total FROM appointments WHERE business_id=? GROUP BY status",
                (business_id,),
            ).fetchall()
        }
        recurring = connection.execute(
            "SELECT COUNT(*) FROM (SELECT phone FROM appointments WHERE business_id=? AND status='completed' AND phone IS NOT NULL GROUP BY phone HAVING COUNT(*) >= 2)",
            (business_id,),
        ).fetchone()[0]
        points = connection.execute(
            "SELECT COALESCE(SUM(delta),0) FROM points_ledger WHERE business_id=? AND type='earn'",
            (business_id,),
        ).fetchone()[0]
        rewards = connection.execute(
            "SELECT COUNT(*) FROM redemptions WHERE business_id=? AND status='redeemed'",
            (business_id,),
        ).fetchone()[0]
        return {
            "appointments": sum(counts.values()),
            "completed": counts.get("completed", 0),
            "cancelled": counts.get("cancelled", 0),
            "no_show": counts.get("no_show", 0),
            "recurring": recurring,
            "points_awarded": points,
            "rewards_redeemed": rewards,
        }
    finally:
        connection.close()


def _s(settings: Any, key: str, default: Any = "") -> Any:
    """Acceso seguro a una clave de settings (dict o sqlite3.Row).

    sqlite3.Row no soporta .get(); usa [] con try/except KeyError.
    """
    if not settings:
        return default
    try:
        return settings[key] or default
    except (KeyError, IndexError, TypeError):
        return default


def get_onboarding_steps(business_id: int, settings: Any, services: Any) -> list[dict[str, Any]]:
    """Evalúa granularmente cada paso del onboarding.

    Deriva el estado de las tablas existentes — no requiere migración.
    """
    steps = []

    has_business_data = bool(
        _s(settings, "business_name")
        and _s(settings, "business_type")
        and _s(settings, "business_initials")
    )
    steps.append(
        {
            "key": "business_data",
            "label": "Datos del negocio",
            "completed": has_business_data,
            "action_text": "Configurar datos",
            "anchor": "configuracion",
        }
    )

    steps.append(
        {
            "key": "services",
            "label": "Servicios",
            "completed": bool(services),
            "action_text": "Agregar servicios",
            "anchor": "servicios",
        }
    )

    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM weekly_schedules WHERE business_id = ? AND is_open = 1",
            (business_id,),
        ).fetchone()
        open_count = row["n"] if row else 0
    finally:
        connection.close()
    has_open_days = open_count > 0
    steps.append(
        {
            "key": "schedules",
            "label": "Horarios semanales",
            "completed": has_open_days,
            "action_text": "Configurar horarios",
            "anchor": "horarios",
        }
    )

    has_resources = bool(get_resources_scoped(business_id))
    steps.append(
        {
            "key": "resources",
            "label": "Recursos (opcional)",
            "completed": has_resources,
            "skippable": True,
            "action_text": "Agregar recursos",
            "anchor": "recursos",
        }
    )

    knowledge = get_knowledge_scoped(business_id, active_only=True)
    steps.append(
        {
            "key": "knowledge",
            "label": "Información para la IA",
            "completed": len(knowledge) > 0,
            "action_text": "Agregar conocimiento",
            "anchor": "conocimiento",
            "page": True,
        }
    )

    steps.append(
        {
            "key": "smtp",
            "label": "Notificaciones / SMTP",
            "completed": smtp_configured(),
            "action_text": "Ver instrucciones SMTP",
            "anchor": "configuracion",
        }
    )

    return steps


def get_onboarding_state(business_id: int, settings: Any, services: Any) -> dict[str, Any]:
    settings = dict(settings) if settings is not None else None
    steps = get_onboarding_steps(business_id, settings, services)
    completed = [s for s in steps if s["completed"]]
    total = len(steps)
    summary = get_product_summary(business_id)

    return {
        "business_configured": bool(settings and settings.get("business_name")),
        "has_service": bool(services),
        "has_appointment": summary["appointments"] > 0,
        "steps": steps,
        "completed_count": len(completed),
        "total_steps": total,
        "all_completed": len(completed) == total
        or all(s["completed"] or s.get("skippable", False) for s in steps),
        "summary": summary,
    }
