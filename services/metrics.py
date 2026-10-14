"""Panel de métricas del negocio (scoped por business_id).

Agregaciones SQL para el panel `/admin/metricas`:

- Resumen operativo por estado (`confirmed`, `cancelled`, `completed`, `no_show`)
  con comparación contra el período anterior de igual duración.
- Ranking de servicios (estados `confirmed` + `completed`, nombre persistido en
  `appointments.service`).
- Demanda efectiva por día de semana y hora (NO es ocupación).
- Clientes nuevos / recurrentes sobre turnos `completed` con teléfono
  normalizado (ver `services.appointments.normalize_phone`).
- Chat: sesiones nuevas y activas (por actividad real en
  `conversation_messages`), mensajes por rol, derivaciones y resoluciones.
- Fidelización: puntos otorgados, canjes, cuentas e inactivos.

Decisiones documentadas (sin especulación):

- Sin importes monetarios: no hay precio histórico fiable por turno.
- Sin % de ocupación: el modelo no permite calcular capacidad disponible.
- Sin atribución de origen (chat/wizard/admin): no hay relación verificable.
- `conversation_analytics` son contadores acumulados sin segmentación temporal
  fiable: se omiten del panel (ver `OMITTED_METRICS`).
- Una sesión atendida por IA NO se considera resuelta por no pedir humano.

Todas las consultas filtran por `business_id`. Los turnos se filtran por
`appointment_date` (fecha local de la cita). Los eventos con marca temporal
(`conversation_*`, `points_ledger`, `redemptions`, `retention_actions`) usan
intervalos semiabiertos `[inicio, fin)` en UTC derivados de la zona horaria del
negocio (`get_business_timezone`).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database.database import get_connection
from services.appointments import DEFAULT_TIMEZONE, get_business_timezone, normalize_phone

logger = logging.getLogger("turnobot.metrics")

MAX_RANGE_DAYS = 365

VALID_PERIODS = ("today", "last7", "last30", "month", "custom")

# Estados reales del sistema usados en el resumen operativo.
KNOWN_STATUSES = ("confirmed", "cancelled", "completed", "no_show")

# Estados que representan demanda efectiva (ranking de servicios y demanda
# por día/horario). Los cancelados y ausencias no representan demanda atendida
# ni por atender.
DEMAND_STATUSES = ("confirmed", "completed")

# Métricas evaluadas y omitidas deliberadamente, con su motivo.
OMITTED_METRICS = (
    "Preguntas frecuentes del período: `conversation_analytics` guarda "
    "contadores acumulados sin segmentación temporal fiable.",
    "Sesiones 'resueltas por IA': no existe un estado que lo respalde; solo se "
    "informan resoluciones humanas con `resolved_at`.",
    "Ingresos / facturación: los turnos no guardan precio histórico fiable.",
    "Porcentaje de ocupación: el modelo no permite calcular la capacidad real.",
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_WEEKDAY_NAMES = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


# ============================================================
# Utilidades de normalización (SQLite TEXT vs PostgreSQL DATE/TIME/TZ)
# ============================================================


def _as_date_str(value) -> str | None:
    """Normaliza DATE a 'YYYY-MM-DD' (str SQLite o date/datetime PG)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) >= 10 and _ISO_DATE_RE.match(text[:10]):
        return text[:10]
    return None


def _as_time_str(value) -> str | None:
    """Normaliza TIME a 'HH:MM' (str SQLite o time PG)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    text = str(value).strip()
    match = re.match(r"^(\d{1,2}):(\d{2})", text)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def _parse_timestamp_text(text: str) -> datetime | None:
    """Parsea 'YYYY-MM-DD [HH:MM:SS[.ffffff]]' a datetime naive (None si falla)."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _as_naive_utc(value) -> datetime | None:
    """Normaliza un timestamp a datetime naive en UTC.

    SQLite guarda 'YYYY-MM-DD HH:MM:SS' (UTC); PostgreSQL devuelve datetimes
    aware (TIMESTAMPTZ, sesión en UTC). Ambos se comparan como UTC naive.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        result: datetime = value
    else:
        parsed = _parse_timestamp_text(str(value).strip().replace("T", " "))
        if parsed is None:
            return None
        result = parsed
    if result.tzinfo is not None:
        result = result.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return result


def _local_day_bounds_utc(day: date, tz_name: str) -> tuple[str, str]:
    """Límites UTC (strings SQLite/PG) del día local [00:00, 24:00)."""
    try:
        tz = ZoneInfo(tz_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    start_utc = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    end_utc = start_utc + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_utc.strftime(fmt), end_utc.strftime(fmt)


def _resolve_timezone(business_id) -> str:
    try:
        return get_business_timezone(business_id)
    except Exception:
        logger.exception("No se pudo resolver la zona horaria del negocio %s", business_id)
        return DEFAULT_TIMEZONE


# ============================================================
# Períodos y filtros de fecha
# ============================================================


def resolve_period(period, start_raw=None, end_raw=None, today=None, tz_name=None):
    """Valida el filtro de fecha y devuelve el período + su anterior equivalente.

    Returns dict con `start`, `end`, `prev_start`, `prev_end` (dates),
    `days`, `period` y `error` (None si es válido).
    """
    tz_name = tz_name or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    today = today or datetime.now(tz).date()

    period = (period or "last7").strip().lower()
    if period not in VALID_PERIODS:
        return {"error": "El período indicado no es válido."}

    if period == "today":
        start = end = today
    elif period == "last7":
        start, end = today - timedelta(days=6), today
    elif period == "last30":
        start, end = today - timedelta(days=29), today
    elif period == "month":
        start, end = today.replace(day=1), today
    else:  # custom
        start_raw = (start_raw or "").strip()
        end_raw = (end_raw or "").strip()
        if not (_ISO_DATE_RE.match(start_raw) and _ISO_DATE_RE.match(end_raw)):
            return {"error": "Usá fechas con formato AAAA-MM-DD para el rango personalizado."}
        try:
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            return {"error": "Alguna de las fechas indicadas no existe en el calendario."}
        if start > end:
            return {"error": "La fecha inicial no puede ser posterior a la final."}
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            return {"error": f"El rango no puede superar los {MAX_RANGE_DAYS} días naturales."}

    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return {
        "error": None,
        "period": period,
        "start": start,
        "end": end,
        "days": days,
        "prev_start": prev_start,
        "prev_end": prev_end,
    }


def _variation(current, previous):
    """Variación absoluta y porcentual (None si no hay base de comparación)."""
    diff = current - previous
    if previous == 0:
        return {"diff": diff, "pct": None}
    return {"diff": diff, "pct": round(diff / previous * 100, 1)}


# ============================================================
# Consultas agregadas (SQL, siempre scoped por business_id)
# ============================================================


def _status_counts(business_id, start, end, connection=None) -> dict:
    """Conteo por estado en el rango (appointment_date local, inclusivo)."""
    own = connection is None
    connection = connection or get_connection()
    try:
        rows = connection.execute(
            """SELECT status, COUNT(*) AS n FROM appointments
               WHERE business_id = ? AND appointment_date >= ? AND appointment_date <= ?
               GROUP BY status""",
            (business_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}
    finally:
        if own:
            connection.close()


def _top_services(business_id, start, end, limit=10, connection=None) -> list:
    """Ranking de servicios por turnos (confirmed + completed)."""
    own = connection is None
    connection = connection or get_connection()
    try:
        rows = connection.execute(
            """SELECT service, COUNT(*) AS n FROM appointments
               WHERE business_id = ? AND appointment_date >= ? AND appointment_date <= ?
                 AND status IN ('confirmed', 'completed')
               GROUP BY service ORDER BY n DESC, service ASC LIMIT ?""",
            (business_id, start.isoformat(), end.isoformat(), limit),
        ).fetchall()
        ranking = []
        for row in rows:
            raw = row["service"]
            name = str(raw).strip() if raw is not None else ""
            ranking.append(
                {"service": name if name else "(sin especificar)", "count": int(row["n"])}
            )
        return ranking
    finally:
        if own:
            connection.close()


def _demand_cells(business_id, start, end, connection=None) -> dict:
    """Demanda efectiva por (día de semana, hora): {(weekday, 'HH:MM'): n}.

    Incluye estados confirmed + completed. La hora se trunca a la hora exacta
    de inicio del turno ('HH:00'-style bucket por hora de `appointment_time`).
    """
    own = connection is None
    connection = connection or get_connection()
    try:
        rows = connection.execute(
            """SELECT appointment_date, appointment_time, COUNT(*) AS n FROM appointments
               WHERE business_id = ? AND appointment_date >= ? AND appointment_date <= ?
                 AND status IN ('confirmed', 'completed')
               GROUP BY appointment_date, appointment_time""",
            (business_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        cells: dict[tuple[int, str], int] = {}
        for row in rows:
            day = _as_date_str(row["appointment_date"])
            hour = _as_time_str(row["appointment_time"])
            if day is None or hour is None:
                continue
            try:
                weekday = date.fromisoformat(day).weekday()
            except ValueError:
                continue
            bucket = f"{hour[:2]}:00"
            key = (weekday, bucket)
            cells[key] = cells.get(key, 0) + int(row["n"])
        return cells
    finally:
        if own:
            connection.close()


def _completed_by_phone(business_id, connection=None) -> list:
    """Agregado SQL por teléfono crudo de turnos completed (todo el historial).

    Devuelve filas con phone, customer_name, first_date, total y in_period ya
    calculados en SQL; la normalización y fusión de variantes del mismo número
    se hace en Python (`_merge_phones`).
    """
    own = connection is None
    connection = connection or get_connection()
    try:
        rows = connection.execute(
            """SELECT phone, MAX(customer_name) AS customer_name,
                      MIN(appointment_date) AS first_date,
                      MAX(appointment_date) AS last_date,
                      COUNT(*) AS total
               FROM appointments
               WHERE business_id = ? AND status = 'completed'
                 AND phone IS NOT NULL AND phone != ''
               GROUP BY phone""",
            (business_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own:
            connection.close()


def _merge_phones(rows) -> list:
    """Fusiona variantes del mismo número normalizado; excluye no identificables."""
    merged: dict[str, dict] = {}
    for row in rows:
        phone = normalize_phone(row.get("phone") or "")
        if not phone:
            continue
        first = _as_date_str(row.get("first_date"))
        last = _as_date_str(row.get("last_date"))
        if first is None:
            continue
        # Turnos del período: se recalcula sobre el rango con las fechas
        # extremas no alcanza; el conteo en período se obtiene aparte.
        item = merged.setdefault(
            phone,
            {
                "phone": phone,
                "name": (row.get("customer_name") or "").strip(),
                "first_date": first,
                "last_date": last,
                "total": 0,
            },
        )
        item["total"] += int(row.get("total") or 0)
        if first < item["first_date"]:
            item["first_date"] = first
        if last and (not item["last_date"] or last > item["last_date"]):
            item["last_date"] = last
    return list(merged.values())


def _clients(business_id, start, end, today, connection=None) -> dict:
    """Clientes únicos y recurrencia sobre turnos completed con teléfono válido."""
    rows = _completed_by_phone(business_id, connection=connection)
    # Conteo en período por teléfono normalizado (una sola query agregada).
    own = connection is None
    connection = connection or get_connection()
    try:
        in_rows = connection.execute(
            """SELECT phone, COUNT(*) AS n FROM appointments
               WHERE business_id = ? AND status = 'completed'
                 AND appointment_date >= ? AND appointment_date <= ?
                 AND phone IS NOT NULL AND phone != ''
               GROUP BY phone""",
            (business_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        if own:
            connection.close()
    in_period: dict[str, int] = {}
    for row in in_rows:
        phone = normalize_phone(row["phone"] or "")
        if phone:
            in_period[phone] = in_period.get(phone, 0) + int(row["n"])

    merged = _merge_phones(rows)
    start_iso = start.isoformat()
    uniques = new = recurrent = 0
    for item in merged:
        if in_period.get(item["phone"]):
            uniques += 1
            if item["first_date"] >= start_iso:
                new += 1
            else:
                recurrent += 1
    # Inactivos: regla del proyecto (>=2 completados y >=60 días sin volver).
    cutoff = (today - timedelta(days=60)).isoformat()
    inactive = [
        item
        for item in merged
        if item["total"] >= 2 and item["last_date"] and item["last_date"] < cutoff
    ]
    inactive.sort(key=lambda item: item["last_date"] or "")
    return {
        "unique_completed": uniques,
        "new": new,
        "recurrent": recurrent,
        "inactive_count": len(inactive),
        "inactive_sample": [
            {
                "phone": item["phone"],
                "name": item["name"],
                "last_date": item["last_date"],
                "completed": item["total"],
            }
            for item in inactive[:20]
        ],
    }


def _chat(business_id, start_utc, end_utc, connection=None) -> dict:
    """Indicadores de chat respaldados por datos reales del período."""
    own = connection is None
    connection = connection or get_connection()
    try:
        new_sessions = connection.execute(
            """SELECT COUNT(*) AS n FROM conversation_sessions
               WHERE business_id = ? AND created_at >= ? AND created_at < ?""",
            (business_id, start_utc, end_utc),
        ).fetchone()["n"]
        active = connection.execute(
            """SELECT COUNT(DISTINCT cm.session_id) AS n
               FROM conversation_messages cm
               JOIN conversation_sessions cs ON cs.id = cm.session_id
               WHERE cs.business_id = ? AND cm.created_at >= ? AND cm.created_at < ?""",
            (business_id, start_utc, end_utc),
        ).fetchone()["n"]
        msg_rows = connection.execute(
            """SELECT cm.role AS role, COUNT(*) AS n
               FROM conversation_messages cm
               JOIN conversation_sessions cs ON cs.id = cm.session_id
               WHERE cs.business_id = ? AND cm.created_at >= ? AND cm.created_at < ?
               GROUP BY cm.role""",
            (business_id, start_utc, end_utc),
        ).fetchall()
        by_role = {str(row["role"]): int(row["n"]) for row in msg_rows}
        handoffs = connection.execute(
            """SELECT COUNT(*) AS n FROM conversation_sessions
               WHERE business_id = ? AND human_requested_at IS NOT NULL
                 AND human_requested_at >= ? AND human_requested_at < ?""",
            (business_id, start_utc, end_utc),
        ).fetchone()["n"]
        res_rows = connection.execute(
            """SELECT human_requested_at, resolved_at FROM conversation_sessions
               WHERE business_id = ? AND resolved_at IS NOT NULL
                 AND resolved_at >= ? AND resolved_at < ?
                 AND human_requested_at IS NOT NULL""",
            (business_id, start_utc, end_utc),
        ).fetchall()
        durations = []
        for row in res_rows:
            requested = _as_naive_utc(row["human_requested_at"])
            resolved = _as_naive_utc(row["resolved_at"])
            if requested and resolved and resolved >= requested:
                durations.append((resolved - requested).total_seconds())
        avg_resolution = round(sum(durations) / len(durations), 1) if durations else None
        return {
            "new_sessions": int(new_sessions),
            "active_sessions": int(active),
            "messages_total": sum(by_role.values()),
            "messages_by_role": by_role,
            "handoffs": int(handoffs),
            "human_resolved": len(res_rows),
            "avg_resolution_seconds": avg_resolution,
        }
    finally:
        if own:
            connection.close()


def _loyalty(business_id, start_utc, end_utc, connection=None) -> dict:
    """Fidelización: puntos otorgados, canjes, cuentas y acciones de retención."""
    own = connection is None
    connection = connection or get_connection()
    try:
        earned = connection.execute(
            """SELECT COALESCE(SUM(delta), 0) AS pts, COUNT(*) AS n FROM points_ledger
               WHERE business_id = ? AND type = 'earn'
                 AND created_at >= ? AND created_at < ?""",
            (business_id, start_utc, end_utc),
        ).fetchone()
        redeemed = connection.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(points_used), 0) AS pts FROM redemptions
               WHERE business_id = ? AND status = 'redeemed'
                 AND created_at >= ? AND created_at < ?""",
            (business_id, start_utc, end_utc),
        ).fetchone()
        accounts = connection.execute(
            "SELECT COUNT(*) AS n FROM loyalty_accounts WHERE business_id = ?", (business_id,)
        ).fetchone()["n"]
        act_rows = connection.execute(
            """SELECT action_type, COUNT(*) AS n FROM retention_actions
               WHERE business_id = ? AND created_at >= ? AND created_at < ?
               GROUP BY action_type""",
            (business_id, start_utc, end_utc),
        ).fetchall()
        return {
            "points_earned": int(earned["pts"]),
            "earn_events": int(earned["n"]),
            "redemptions": int(redeemed["n"]),
            "points_redeemed": int(redeemed["pts"]),
            "accounts": int(accounts),
            "retention_actions": {str(row["action_type"]): int(row["n"]) for row in act_rows},
        }
    finally:
        if own:
            connection.close()


# ============================================================
# Entrada principal
# ============================================================


def get_metrics(business_id, start, end, prev_start, prev_end, today=None) -> dict:
    """Calcula todas las métricas del panel para el rango [start, end].

    No filtra por origen de reserva ni calcula dinero u ocupación.
    """
    tz_name = _resolve_timezone(business_id)
    tz = ZoneInfo(tz_name)
    today = today or datetime.now(tz).date()

    connection = get_connection()
    try:
        counts = _status_counts(business_id, start, end, connection)
        prev_counts = _status_counts(business_id, prev_start, prev_end, connection)
        total = sum(counts.values())
        prev_total = sum(prev_counts.values())

        by_status = {status: counts.get(status, 0) for status in KNOWN_STATUSES}
        unknown = total - sum(by_status.values())
        prev_by_status = {status: prev_counts.get(status, 0) for status in KNOWN_STATUSES}

        cancel_rate = round(by_status["cancelled"] / total * 100, 1) if total else None
        no_show_base = by_status["completed"] + by_status["no_show"]
        absence_rate = round(by_status["no_show"] / no_show_base * 100, 1) if no_show_base else None

        start_utc, end_utc = _local_day_bounds_utc(start, tz_name)
        _, next_utc = _local_day_bounds_utc(end, tz_name)
        end_bound = next_utc

        services = _top_services(business_id, start, end, connection=connection)
        cells = _demand_cells(business_id, start, end, connection=connection)
        clients = _clients(business_id, start, end, today, connection=connection)
        chat = _chat(business_id, start_utc, end_bound, connection=connection)
        loyalty = _loyalty(business_id, start_utc, end_bound, connection=connection)
    finally:
        connection.close()

    # Demanda agregada para la plantilla (sin cargar turnos individuales).
    by_weekday = [0] * 7
    by_hour: dict[str, int] = {}
    for (weekday, bucket), count in cells.items():
        by_weekday[weekday] += count
        by_hour[bucket] = by_hour.get(bucket, 0) + count
    peak = max(cells.items(), key=lambda kv: kv[1], default=None)
    hours = sorted(by_hour)

    return {
        "summary": {
            "total": total,
            "by_status": by_status,
            "unknown": unknown,
            "cancel_rate": cancel_rate,
            "absence_rate": absence_rate,
        },
        "previous": {"total": prev_total, "by_status": prev_by_status},
        "variations": {
            "total": _variation(total, prev_total),
            **{
                status: _variation(by_status[status], prev_by_status[status])
                for status in KNOWN_STATUSES
            },
        },
        "services": services,
        "demand": {
            "cells": [
                {"weekday": weekday, "hour": bucket, "count": count}
                for (weekday, bucket), count in sorted(cells.items())
            ],
            "by_weekday": [
                {"weekday": index, "name": _WEEKDAY_NAMES[index], "count": by_weekday[index]}
                for index in range(7)
            ],
            "by_hour": [{"hour": hour, "count": by_hour[hour]} for hour in hours],
            "peak": (
                {
                    "weekday": peak[0][0],
                    "weekday_name": _WEEKDAY_NAMES[peak[0][0]],
                    "hour": peak[0][1],
                    "count": peak[1],
                }
                if peak
                else None
            ),
            "statuses": list(DEMAND_STATUSES),
        },
        "clients": clients,
        "chat": chat,
        "loyalty": loyalty,
        "omitted": list(OMITTED_METRICS),
    }
