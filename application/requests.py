"""Helpers base de request (Bloque 5B, Paso 5).

get_client_ip, json_object, _rate_limit_key y _human_reschedule_error
extraídos de app.py, manteniendo lógica idéntica.
"""

from flask import request


def get_client_ip():
    return request.remote_addr or "unknown"


def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _rate_limit_key(endpoint, client_ip, business_id=None, user_id=None):
    # Values come only from Flask's resolved request/session context, never
    # from request data supplied by the caller.
    scope = f"user:{user_id}:business:{business_id}" if user_id else f"ip:{client_ip}:business:{business_id}"
    return f"{endpoint}:{scope}"


def _human_reschedule_error(reason):
    reasons = {
        "occupied": "ese horario ya está ocupado.",
        "past_date": "no podés reprogramar a una fecha que ya pasó.",
        "closed_day": "ese día estamos cerrados.",
        "invalid_date": "la fecha no es válida.",
        "invalid_time": "el horario no es válido.",
        "not_found": "no se encontró el turno.",
        "invalid_phone": "el teléfono no es válido.",
    }
    return reasons.get(reason, "intentá nuevamente.")