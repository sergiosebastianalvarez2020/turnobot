"""Helpers base de request (Bloque 5B, Paso 5).

get_client_ip, json_object, _rate_limit_key y _human_reschedule_error
extraídos de app.py, manteniendo lógica idéntica.
"""

import time

from flask import jsonify, request, session

# Mínimo tiempo de llenado del formulario de reserva (segundos).
# Justificación: el wizard exige seleccionar servicio, elegir fecha/horario,
# escribir nombre y teléfono, revisar el resumen y confirmar. Con autocompletado
# del navegador un humano razonablemente rápido tarda ~3s; 2s es un umbral
# conservador que no bloquea usuarios legítimos pero sí envíos automatizados
# inmediatos (bot headless que POSTea sin renderizar el formulario).
MIN_FORM_SECONDS = 2

# Cantidad máxima de renders de formulario recordados por sesión (multi-pestaña).
MAX_TRACKED_FORM_RENDERS = 5


def get_client_ip():
    return request.remote_addr or "unknown"


def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _rate_limit_key(endpoint, client_ip, business_id=None, user_id=None):
    # Values come only from Flask's resolved request/session context, never
    # from request data supplied by the caller.
    scope = (
        f"user:{user_id}:business:{business_id}"
        if user_id
        else f"ip:{client_ip}:business:{business_id}"
    )
    return f"{endpoint}:{scope}"


def register_form_render():
    """Registra en la sesión el timestamp de render del formulario de reserva.

    Se guarda una lista acotada (multi-pestaña: cada render agrega una entrada)
    porque el POST posterior se valida CONTRA este valor del servidor: nunca se
    confía en el timestamp que viaja en el cuerpo de la request.

    Returns:
        int: timestamp epoch (segundos) acordado para este render.
    """
    now = int(time.time())
    renders = session.get("reserva_form_rendered_at") or []
    if not isinstance(renders, list):
        renders = []
    renders.append(now)
    session["reserva_form_rendered_at"] = renders[-MAX_TRACKED_FORM_RENDERS:]
    return now


def is_honeypot_triggered(data):
    """True si un campo honeypot (nombre con prefijo `hp_`) vino completado.

    El nombre del campo es aleatorio por render (`hp_<token>`), así que no hay
    un nombre fijo que un bot pueda identificar y dejar vacío a propósito. Los
    usuarios humanos nunca lo completan (es un input oculto, sin autocompletado).

    Alcance: protege el flujo basado en formulario (wizard). NO es una garantía
    contra automatización directa de `/api/reservar`: un cliente que no envía el
    campo simplemente no lo dispara.
    """
    if not isinstance(data, dict):
        return False
    return any(key.startswith("hp_") and str(value).strip() for key, value in data.items())


def check_form_timing(data):
    """Valida que el formulario no se haya enviado demasiado rápido.

    El timestamp renderizado por el servidor se guarda en la sesión; el valor
    que viaja en el cuerpo solo se acepta si coincide con uno de los renders
    registrados para esta sesión. Así un valor inventado por el cliente no
    pasa la verificación, y un usuario con varias pestañas abiertas (cada una
    con su timestamp) no sufre falsos positivos.

    Returns:
        tuple: (ok: bool, reason: str|None)
    """
    renders = session.get("reserva_form_rendered_at")
    if not renders or not isinstance(renders, list):
        # Flujo sin render del wizard (API programática directa): no aplica.
        return True, None

    submitted = data.get("form_rendered_at") if isinstance(data, dict) else None
    if submitted is None:
        return False, "missing_timestamp"
    try:
        submitted = int(submitted)
    except (TypeError, ValueError):
        return False, "invalid_timestamp"

    if submitted not in {int(entry) for entry in renders}:
        # No corresponde a ningún formulario que esta sesión haya renderizado.
        return False, "unregistered_timestamp"

    elapsed = int(time.time()) - submitted
    if elapsed < MIN_FORM_SECONDS:
        return False, "too_fast"
    return True, None


def contact_limit_response(kind):
    """Respuesta HTTP uniforme del cupo de reservas por contacto (429).

    Una sola implementación compartida por el chequeo anticipado de las rutas y
    por el enforcement dentro de `create_appointment`/`reschedule_appointment`,
    para que ambos caminos devuelvan exactamente el mismo cuerpo.

    `kind` es 'phone' o 'email': indica QUÉ dimensión alcanzó el cupo, nunca el
    valor del contacto (no se enumera PII).
    """
    response = jsonify(
        {
            "success": False,
            "code": "CONTACT_LIMIT",
            "reason": kind,
            "error": "Alcanzaste el máximo de reservas activas con estos datos de contacto. "
            "Cancelá o consultá tus turnos para poder reservar de nuevo.",
        }
    )
    response.status_code = 429
    return response


def _human_reschedule_error(reason):
    reasons = {
        "occupied": "ese horario ya está ocupado.",
        "past_date": "no podés reprogramar a una fecha que ya pasó.",
        "closed_day": "ese día estamos cerrados.",
        "invalid_date": "la fecha no es válida.",
        "invalid_time": "el horario no es válido.",
        "not_found": "no se encontró el turno.",
        "invalid_phone": "el teléfono no es válido.",
        "contact_limit": (
            "alcanzaste el máximo de reservas activas con estos datos de contacto. "
            "Cancelá una reserva existente o contactá al negocio para seguir reservando."
        ),
    }
    return reasons.get(reason, "intentá nuevamente.")
