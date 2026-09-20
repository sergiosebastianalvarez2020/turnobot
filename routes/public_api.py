"""
Módulo de rutas públicas: API de servicios, recursos, puntos, disponibilidad,
turnos, reservas, cancelación, reprogramación, chat y gestión de turnos públicos.

Contiene las view functions para:
- /chat                              -> endpoint: chat
- /api/conversations/<public_token>/messages -> endpoint: public_conversation_messages
- /api/servicios                     -> endpoint: api_servicios
- /b/<slug>/api/servicios            -> endpoint: api_servicios
- /api/recursos                      -> endpoint: api_recursos
- /b/<slug>/api/recursos             -> endpoint: api_recursos
- /api/puntos                        -> endpoint: api_puntos
- /b/<slug>/api/puntos               -> endpoint: api_puntos
- /api/disponibilidad/<fecha>        -> endpoint: api_disponibilidad
- /b/<slug>/api/disponibilidad/<fecha> -> endpoint: api_disponibilidad
- /api/turnos                        -> endpoint: api_turnos
- /b/<slug>/api/turnos               -> endpoint: api_turnos
- /api/reservar                      -> endpoint: api_reservar
- /b/<slug>/api/reservar             -> endpoint: api_reservar
- /b/<slug>/turno/<token>            -> endpoint: public_manage_turno
- /api/cancelar                      -> endpoint: api_cancelar
- /b/<slug>/api/cancelar             -> endpoint: api_cancelar
- /api/reprogramar                   -> endpoint: api_reprogramar
- /b/<slug>/api/reprogramar          -> endpoint: api_reprogramar

Estas rutas NO usan Blueprint registrado. Las view functions se
registran vía app.add_url_rule() en routes/__init__.py preservando
los endpoint names globales.
"""

import logging

from flask import (
    abort,
    g,
    jsonify,
    render_template,
    request,
)
from services.appointments import (
    cancel_appointment,
    create_appointment,
    get_appointment_by_token,
    get_available_times,
    get_customer_appointments,
    normalize_phone,
    reschedule_appointment,
    validate_email,
)
from services.conversations import (
    get_conversation_messages_by_public_token_scoped,
)
from services import loyalty
from services.notifications import notifications_enabled
from database.database import (
    ensure_loyalty_settings_scoped,
    get_active_services_scoped,
    get_resource_scoped,
    get_resources_scoped,
)

logger = logging.getLogger(__name__)


def chat():
    from app import (
        ask_ai,
        get_current_business_id,
        get_client_ip,
        is_chat_request_allowed,
        is_chat_phone_request_allowed,
        json_object,
        MAX_HISTORY_MESSAGES,
        MAX_HISTORY_CONTENT_LENGTH,
        MAX_MESSAGE_LENGTH,
    )

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "El formato enviado no es válido."}), 400

        if not is_chat_request_allowed(get_client_ip(), get_current_business_id()):
            return jsonify({
                "success": False,
                "code": "RATE_LIMITED",
                "error": "Esperá un momento antes de enviar otro mensaje."
            }), 429

        raw_message = data.get("message", "")
        if not isinstance(raw_message, str):
            return jsonify({"success": False, "error": "El mensaje debe ser texto."}), 400

        message = raw_message.strip()

        conversation = data.get(
            "conversation",
            []
        )

        if not isinstance(conversation, list) or len(conversation) > MAX_HISTORY_MESSAGES:
            return jsonify({"success": False, "error": "El historial no es válido."}), 400

        if any(
            not isinstance(item, dict)
            or item.get("role") not in ("user", "assistant")
            or not isinstance(item.get("content"), str)
            or len(item["content"]) > MAX_HISTORY_CONTENT_LENGTH
            for item in conversation
        ):
            return jsonify({"success": False, "error": "El historial no es válido."}), 400

        if not message:

            return jsonify({
                "success": False,
                "error": "No se recibió ningún mensaje."
            }), 400

        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({
                "success": False,
                "error": "El mensaje es demasiado largo."
            }), 400

        customer_phone_raw = data.get("customer_phone", "")
        customer_phone = normalize_phone(customer_phone_raw) if isinstance(customer_phone_raw, str) else ""
        customer_name = data.get("customer_name", "").strip() if isinstance(data.get("customer_name"), str) else ""
        customer_email = data.get("customer_email", "").strip() if isinstance(data.get("customer_email"), str) else ""

        if customer_phone and not is_chat_phone_request_allowed(customer_phone, get_current_business_id()):
            return jsonify({
                "success": False,
                "code": "RATE_LIMITED",
                "error": "Esperá un momento antes de enviar otro mensaje."
            }), 429


        response, session_id, public_token = ask_ai(
            message,
            conversation,
            business_id=get_current_business_id(),
            customer_phone=customer_phone,
            customer_name=customer_name,
            customer_email=customer_email,
        )


        payload = {
            "success": True,
            "response": response
        }

        if session_id:
            payload["session_id"] = session_id

        if public_token:
            payload["public_token"] = public_token

        return jsonify(payload)


    except Exception as error:

        logger.exception("Error procesando /chat")

        return jsonify({
            "success": False,
            "error": "No se pudo procesar la consulta."
        }), 500


def public_conversation_messages(public_token):
    from app import get_current_business_id
    business_id = get_current_business_id()
    if business_id is None:
        return jsonify({
            "success": False,
            "error": "No hay un negocio activo para esta solicitud."
        }), 404

    messages = get_conversation_messages_by_public_token_scoped(public_token, business_id)
    if messages is None:
        return jsonify({
            "success": False,
            "error": "Conversación no encontrada."
        }), 404

    return jsonify({
        "success": True,
        "messages": messages
    })


# ============================================================

def _get_public_services_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
    )
    if not is_api_request_allowed(get_client_ip(), "api:servicios", business_id):
        return jsonify({"success": False, "code": "RATE_LIMITED", "error": "Demasiadas solicitudes. Esperá un momento."}), 429
    if business_id is None:
        return jsonify({
            "success": False,
            "code": "NOT_FOUND",
            "error": "No hay un negocio activo para esta solicitud."
        }), 404

    services = get_active_services_scoped(business_id)
    servicios = []
    for row in services:
        servicios.append({
            "nombre": row["name"],
            "precio": row["price"],
            "duracion": row["duration"]
        })

    return jsonify({
        "success": True,
        "servicios": servicios
    })


def api_servicios():
    from app import get_current_business_id
    return _get_public_services_response(get_current_business_id())


def business_api_servicios(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    return _get_public_services_response(get_current_business_id())


# ============================================================

def _get_public_resources_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
    )
    """Devuelve los recursos activos del negocio actual."""
    if not is_api_request_allowed(get_client_ip(), "api:recursos", business_id):
        return jsonify({"success": False, "code": "RATE_LIMITED", "error": "Demasiadas solicitudes. Esperá un momento."}), 429
    if business_id is None:
        return jsonify({"success": False, "code": "NOT_FOUND", "error": "No hay un negocio activo para esta solicitud."}), 404

    resources = get_resources_scoped(business_id, only_active=True)
    recursos = []
    for row in resources:
        recursos.append({
            "id": row["id"],
            "nombre": row["name"]
        })

    return jsonify({
        "success": True,
        "recursos": recursos
    })


def api_recursos():
    from app import get_current_business_id
    return _get_public_resources_response(get_current_business_id())


def business_api_recursos(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    return _get_public_resources_response(get_current_business_id())


# ============================================================

def _get_public_points_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
    )
    """Saldo de puntos del cliente para una consulta pública del negocio.

    Solo devuelve el saldo si fidelización está habilitada y el phone es válido.
    No expone el ledger completo (es info sensible; en v1 el cliente ve el saldo,
    el historial detallado queda para el panel admin).
    """
    if not is_api_request_allowed(get_client_ip(), "api:puntos", business_id):
        return jsonify({"success": False, "code": "RATE_LIMITED", "error": "Demasiadas solicitudes. Esperá un momento."}), 429
    if business_id is None:
        return jsonify({"success": False, "code": "NOT_FOUND", "error": "No hay un negocio activo para esta solicitud."}), 404

    settings = ensure_loyalty_settings_scoped(business_id)
    if not settings or not settings.get("enabled"):
        return jsonify({"success": True, "enabled": False, "balance": 0})

    phone = (request.args.get("phone") or "").strip()
    account = loyalty.get_account(business_id, phone) if phone else None
    return jsonify({
        "success": True,
        "enabled": True,
        "points_per_completed": settings.get("points_per_completed_appointment"),
        "balance": account["points_balance"] if account else 0,
    })


def api_puntos():
    from app import get_current_business_id
    return _get_public_points_response(get_current_business_id())


def business_api_puntos(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)
    g.current_business = business
    return _get_public_points_response(get_current_business_id())

# ============================================================

def _get_public_availability_response(fecha, business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
    )
    if not is_api_request_allowed(get_client_ip(), "api:disponibilidad", business_id):
        return jsonify({"success": False, "code": "RATE_LIMITED", "error": "Demasiadas solicitudes. Esperá un momento."}), 429

    # resource_id opcional: permite filtrar disponibilidad por recurso
    resource_id_raw = request.args.get("resource_id")
    resource_id = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "resource_id inválido."}), 400

    try:
        horarios = get_available_times(fecha, business_id, request.args.get("servicio"), resource_id)
        return jsonify({
            "success": True,
            "fecha": fecha,
            "horarios_disponibles": horarios
        })
    except Exception:
        logger.exception("Error consultando disponibilidad")
        return jsonify({
            "success": False,
            "error": "No se pudo consultar la disponibilidad."
        }), 500


def api_disponibilidad(fecha):
    from app import get_current_business_id
    return _get_public_availability_response(fecha, get_current_business_id())


def business_api_disponibilidad(slug, fecha):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    return _get_public_availability_response(fecha, get_current_business_id())


# ============================================================

def _get_public_appointments_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
    )
    if not is_api_request_allowed(get_client_ip(), "api:turnos", business_id):
        return jsonify({"success": False, "code": "RATE_LIMITED", "error": "Demasiadas solicitudes. Esperá un momento."}), 429

    nombre = request.args.get(
        "nombre",
        ""
    ).strip()

    telefono = request.args.get("telefono", "").strip()


    if not nombre:

        return jsonify({
            "success": False,
            "error": "El nombre es obligatorio."
        }), 400

    if not telefono:
        return jsonify({
            "success": False,
            "error": "El teléfono es obligatorio para consultar tus turnos."
        }), 400


    try:

        turnos = get_customer_appointments(
            nombre,
            telefono,
            business_id,
        )


        return jsonify({
            "success": True,
            "turnos": turnos
        })


    except Exception:

        logger.exception("Error buscando turnos")

        return jsonify({
            "success": False,
            "error": "No se pudieron consultar los turnos."
        }), 500


def api_turnos():
    from app import get_current_business_id
    return _get_public_appointments_response(get_current_business_id())


def business_api_turnos(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _get_public_appointments_response(business_id)


# ============================================================

def _create_public_appointment_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
        json_object,
        send_business_confirmation_email,
        send_confirmation_email,
    )

    if not is_api_request_allowed(get_client_ip(), "api:reservar", business_id):
        return jsonify({
            "success": False,
            "code": "RATE_LIMITED",
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        nombre = data.get("nombre", "")
        telefono = data.get("telefono", "")
        if not isinstance(nombre, str) or not isinstance(telefono, str):
            return jsonify({"success": False, "error": "Nombre y teléfono deben ser texto."}), 400
        nombre = nombre.strip()

        telefono = telefono.strip()

        email = data.get("email", "")
        if not isinstance(email, str):
            email = ""
        email = email.strip()

        servicio = data.get(
            "servicio"
        )

        fecha = data.get(
            "fecha"
        )

        hora = data.get(
            "hora"
        )


        # ----------------------------------------------------
        # VALIDACIONES
        # ----------------------------------------------------

        if not nombre:

            return jsonify({
                "success": False,
                "error": "El nombre y apellido son obligatorios."
            }), 400


        if not telefono:

            return jsonify({
                "success": False,
                "error": "El teléfono es obligatorio."
            }), 400


        if email and not validate_email(email):

            return jsonify({
                "success": False,
                "error": "El email no es válido."
            }), 400


        if not email and notifications_enabled(business_id):

            return jsonify({
                "success": False,
                "code": "email_required",
                "reason": "email_required",
                "error": "El email es obligatorio para poder enviarte la confirmación del turno."
            }), 400


        services = get_active_services_scoped(business_id) if business_id is not None else []
        allowed_services = {row["name"] for row in services}

        if servicio not in allowed_services:

            return jsonify({
                "success": False,
                "error": "El servicio seleccionado no es válido."
            }), 400


        if not fecha or not hora:

            return jsonify({
                "success": False,
                "error": "La fecha y el horario son obligatorios."
            }), 400


        # ----------------------------------------------------
        # VALIDAR RECURSO (opcional)
        # ----------------------------------------------------

        resource_id_raw = data.get("resource_id")
        resource_id = None
        if resource_id_raw is not None:
            try:
                resource_id = int(resource_id_raw)
                if resource_id <= 0:
                    resource_id = None
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "resource_id inválido."}), 400


        # ----------------------------------------------------
        # CREAR TURNO
        # ----------------------------------------------------

        resultado = create_appointment(

            customer_name=nombre,

            phone=telefono,

            service=servicio,

            appointment_date=fecha,

            appointment_time=hora,

            business_id=business_id,

            email=email or None,

            resource_id=resource_id,
        )


        # ----------------------------------------------------
        # FECHA PASADA
        # ----------------------------------------------------

        if resultado.get("reason") == "past_date":

            return jsonify({
                "success": False,
                "code": "past_date",
                "reason": "past_date",
                "error": "No podés reservar una fecha que ya pasó."
            }), 400


        # ----------------------------------------------------
        # DÍA CERRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "closed_day":

            return jsonify({
                "success": False,
                "code": "closed_day",
                "reason": "closed_day",
                "error": "Ese día estamos cerrados."
            }), 400


        # ----------------------------------------------------
        # FECHA INVÁLIDA
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_date":

            return jsonify({
                "success": False,
                "code": "invalid_date",
                "reason": "invalid_date",
                "error": "La fecha seleccionada no es válida."
            }), 400


        # ----------------------------------------------------
        # HORARIO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_time":

            return jsonify({
                "success": False,
                "code": "invalid_time",
                "reason": "invalid_time",
                "error": "El horario seleccionado no es válido."
            }), 400


        # ----------------------------------------------------
        # HORARIO OCUPADO
        # ----------------------------------------------------

        if resultado.get("reason") == "occupied":

            return jsonify({
                "success": False,
                "code": "occupied",
                "reason": "occupied",
                "error": "Ese horario ya está ocupado."
            }), 400


        # ----------------------------------------------------
        # NOMBRE INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_name":

            return jsonify({
                "success": False,
                "code": "invalid_name",
                "reason": "invalid_name",
                "error": "El nombre y apellido deben tener al menos 2 caracteres."
            }), 400


        # ----------------------------------------------------
        # TELÉFONO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_phone":

            return jsonify({
                "success": False,
                "code": "invalid_phone",
                "reason": "invalid_phone",
                "error": "El teléfono debe tener al menos 7 dígitos."
            }), 400


        # ----------------------------------------------------
        # RESERVA CORRECTA
        # ----------------------------------------------------

        if resultado.get("success"):

            # ----------------------------------------------------
            # CONFIRMACIÓN POR EMAIL (no bloqueante para la reserva)
            # ----------------------------------------------------

            try:
                current = getattr(g, "current_business", None) or {}
                slug = current.get("slug") or None
                base_url = request.url_root.rstrip("/")
                appointment_data = {
                    "id": resultado.get("appointment_id"),
                    "customer_name": resultado.get("customer_name"),
                    "customer_email": resultado.get("customer_email"),
                    "service": resultado.get("service"),
                    "appointment_date": resultado.get("appointment_date"),
                    "appointment_time": resultado.get("appointment_time"),
                    "appointment_end": resultado.get("appointment_end"),
                }
                send_confirmation_email(
                    business_id,
                    appointment_data,
                    management_token=resultado.get("management_token"),
                    slug=slug,
                    public_base_url=base_url,
                )
                # EMAIL 5: aviso al negocio (scoped, no bloqueante)
                send_business_confirmation_email(business_id, appointment_data)
            except Exception:
                logger.exception("Error enviando confirmación de turno")

            response_data = {
                "success": True,
                "appointment_id": resultado.get("appointment_id"),
                "management_token": resultado.get("management_token"),
                "message": "El turno fue reservado correctamente."
            }
            if resultado.get("resource_id"):
                response_data["resource_id"] = resultado["resource_id"]
                # Obtener el nombre del recurso para la respuesta
                resource = get_resource_scoped(resultado["resource_id"], business_id)
                if resource:
                    response_data["resource_nombre"] = resource["name"]

            return jsonify(response_data), 201

        if resultado.get("reason") == "past_time":
            return jsonify({
                "success": False,
                "code": "past_time",
                "reason": "past_time",
                "error": "Ese horario ya pasó.",
            }), 400

        if resultado.get("reason") == "invalid_service":
            return jsonify({
                "success": False,
                "code": "invalid_service",
                "reason": "invalid_service",
                "error": "El servicio seleccionado no está disponible.",
            }), 400

        if resultado.get("reason") == "invalid_resource":
            return jsonify({
                "success": False,
                "code": "invalid_resource",
                "reason": "invalid_resource",
                "error": "El recurso seleccionado no es válido o no está disponible.",
            }), 400


        # ----------------------------------------------------
        # ERROR DESCONOCIDO
        # ----------------------------------------------------

        return jsonify({
            "success": False,
            "code": "INTERNAL_ERROR",
            "error": "No se pudo realizar la reserva."
        }), 500


    except Exception as error:

        logger.exception("Error reservando turno")

        return jsonify({
            "success": False,
            "code": "INTERNAL_ERROR",
            "error": "No se pudo realizar la reserva."
        }), 500


def api_reservar():
    from app import get_current_business_id
    return _create_public_appointment_response(get_current_business_id())


def business_api_reservar(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _create_public_appointment_response(business_id)


# ============================================================

def public_manage_turno(slug, token):
    from app import (
        _human_reschedule_error,
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None or business.get("slug") != slug:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    appointment_id = request.args.get("id") or (request.form.get("id") or request.view_args.get("id"))
    try:
        appointment_id = int(appointment_id)
    except (TypeError, ValueError):
        appointment_id = None

    if appointment_id is None or appointment_id <= 0:
        return render_template(
            "gestionar_turno.html",
            business=business,
            appointment=None,
            error="El enlace no es válido o el turno no existe.",
            message=None,
        ), 404

    appointment = get_appointment_by_token(appointment_id, business_id, token)
    if appointment is not None:
        appointment = dict(appointment)
    if appointment is None:
        return render_template(
            "gestionar_turno.html",
            business=business,
            appointment=None,
            error="El enlace no es válido o el turno no existe.",
            message=None,
        ), 404

    error = None
    message = None

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "cancelar":
            if cancel_appointment(
                appointment_id,
                appointment.get("phone") or "",
                business_id,
                management_token=token,
            ):
                message = "Tu turno fue cancelado correctamente."
                appointment = get_appointment_by_token(appointment_id, business_id, token)
                if appointment is not None:
                    appointment = dict(appointment)
            else:
                error = "No se pudo cancelar el turno. Puede que ya haya sido cancelado."

        elif action == "reprogramar":
            nueva_fecha = (request.form.get("fecha") or "").strip()
            nueva_hora = (request.form.get("hora") or "").strip()
            res = reschedule_appointment(
                appointment_id,
                nueva_fecha,
                nueva_hora,
                appointment.get("phone") or "",
                business_id,
                management_token=token,
            )
            if res.get("success"):
                message = "Tu turno fue reprogramado correctamente."
                appointment = get_appointment_by_token(appointment_id, business_id, token)
                if appointment is not None:
                    appointment = dict(appointment)
            else:
                error = "No se pudo reprogramar el turno: " + _human_reschedule_error(res.get("reason"))

        elif action == "canjear":
            settings = ensure_loyalty_settings_scoped(business_id)
            result = loyalty.redeem(
                business_id,
                loyalty.get_account(business_id, appointment.get("phone") or "") ["id"],
                request.form.get("reward_id"),
                request.form.get("idempotency_key"),
            ) if settings and settings.get("enabled") and loyalty.get_account(business_id, appointment.get("phone") or "") else {"success": False, "reason": "disabled"}
            message = "Canje realizado correctamente." if result["success"] else "No se pudo realizar el canje: " + result["reason"]
            if not result["success"]:
                error, message = message, None

    return render_template(
        "gestionar_turno.html",
        business=business,
        appointment=appointment,
        error=error,
        message=message,
        loyalty=_loyalty_public_context(business_id, appointment),
    )


def _loyalty_public_context(business_id, appointment):
    """Contexto público de fidelización para la vista del cliente.

    Devuelve un dict que la plantilla usa para mostrar el bloque "Mis puntos".
    Si fidelización está desactivada o no hay cuenta, devuelve un estado minimal
    para no inventar puntos de un sistema apagado.
    """
    if appointment is None:
        return {"enabled": False, "balance": 0, "settings": None}
    settings = ensure_loyalty_settings_scoped(business_id)
    enabled = bool(settings and settings.get("enabled"))
    if not enabled:
        return {"enabled": False, "balance": 0, "settings": settings}
    phone = (appointment.get("phone") or "").strip()
    balance = loyalty.get_balance(business_id, phone) if phone else 0
    return {
        "enabled": True,
        "balance": balance,
        "points_per_completed": settings.get("points_per_completed_appointment") if settings else 0,
        "rewards": loyalty.list_rewards(business_id, active_only=True),
        "idempotency_key": secrets.token_urlsafe(24),
    }


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


# ============================================================

def _cancel_public_appointment_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
        json_object,
    )
    if not is_api_request_allowed(get_client_ip(), "api:cancelar", business_id):
        return jsonify({
            "success": False,
            "code": "RATE_LIMITED",
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        appointment_id = data.get(
            "appointment_id"
        )

        telefono = data.get("telefono", "").strip()


        if not appointment_id:

            return jsonify({
                "success": False,
                "error": "Falta el ID del turno."
            }), 400

        management_token = data.get("management_token")
        if management_token is not None and (not isinstance(management_token, str) or not management_token):
            return jsonify({"success": False, "error": "No pudimos validar ese turno."}), 400

        telefono = data.get("telefono", "").strip()
        customer_name = data.get("customer_name", "").strip()

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400

        if not management_token:
            return jsonify({"success": False, "error": "El token de gestión es obligatorio para cancelar un turno."}), 400

        if not customer_name:
            return jsonify({"success": False, "error": "El nombre del cliente es obligatorio."}), 400


        resultado = cancel_appointment(
            appointment_id,
            telefono,
            business_id,
            management_token,
            customer_name if customer_name else None,
        )


        if not resultado:

            return jsonify({
                "success": False,
                "error": "No pudimos encontrar ese turno con los datos indicados. Verificá tu nombre y teléfono e intentá nuevamente."
            }), 400


        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue cancelado correctamente."
        })


    except Exception:

        logger.exception("Error cancelando turno")

        return jsonify({
            "success": False,
            "code": "INTERNAL_ERROR",
            "error": "No se pudo cancelar el turno."
        }), 500


def api_cancelar():
    from app import get_current_business_id
    return _cancel_public_appointment_response(get_current_business_id())


def business_api_cancelar(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _cancel_public_appointment_response(business_id)


# ============================================================

def _get_public_reschedule_response(business_id):
    from app import (
        get_client_ip,
        is_api_request_allowed,
        json_object,
    )

    if not is_api_request_allowed(get_client_ip(), "api:reprogramar", business_id):
        return jsonify({
            "success": False,
            "code": "RATE_LIMITED",
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400


        appointment_id = data.get(
            "appointment_id"
        )

        nueva_fecha = data.get(
            "nueva_fecha"
        )

        nueva_hora = data.get(
            "nueva_hora"
        )

        telefono = data.get("telefono", "").strip()


        if not appointment_id:

            return jsonify({
                "success": False,
                "error": "Falta el ID del turno."
            }), 400

        management_token = data.get("management_token")
        if management_token is not None and (not isinstance(management_token, str) or not management_token):
            return jsonify({"success": False, "error": "No pudimos validar ese turno."}), 400

        telefono = data.get("telefono", "").strip()
        customer_name = data.get("customer_name", "").strip()

        if not nueva_fecha or not nueva_hora:
            return jsonify({
                "success": False,
                "error": "La nueva fecha y hora son obligatorias."
            }), 400

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400

        if not management_token:
            return jsonify({"success": False, "error": "El token de gestión es obligatorio para reprogramar un turno."}), 400

        if not customer_name:
            return jsonify({"success": False, "error": "El nombre del cliente es obligatorio."}), 400


        resultado = reschedule_appointment(

            appointment_id,

            nueva_fecha,

            nueva_hora,

            telefono,

            business_id,
            management_token,
            customer_name if customer_name else None,
        )


        # ----------------------------------------------------
        # HORARIO OCUPADO
        # ----------------------------------------------------

        if resultado.get("reason") == "occupied":

            return jsonify({

                "success": False,

                "code": "occupied",
                "reason": "occupied",

                "error": "El nuevo horario ya está ocupado."
            })


        # ----------------------------------------------------
        # TURNO NO ENCONTRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "not_found":

            return jsonify({

                "success": False,

                "code": "not_found",
                "reason": "not_found",

                "error": "No pudimos encontrar ese turno con los datos indicados. Verificá tu nombre y teléfono e intentá nuevamente."
            })


        # ----------------------------------------------------
        # ID DE TURNO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_appointment_id":

            return jsonify({
                "success": False,
                "code": "invalid_appointment_id",
                "reason": "invalid_appointment_id",
                "error": "El ID del turno no es válido.",
            }), 400


        # ----------------------------------------------------
        # HORARIO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_time":

            return jsonify({

                "success": False,

                "code": "invalid_time",
                "reason": "invalid_time",

                "error": "El horario seleccionado no es válido."
            }), 400

        if resultado.get("reason") == "past_time":
            return jsonify({
                "success": False,
                "code": "past_time",
                "reason": "past_time",
                "error": "Ese horario ya pasó.",
            }), 400
                # ----------------------------------------------------
        # FECHA PASADA
        # ----------------------------------------------------

        if resultado.get("reason") == "past_date":

            return jsonify({

                "success": False,

                "code": "past_date",
                "reason": "past_date",

                "error": "No podés reprogramar el turno para una fecha que ya pasó."
            }), 400


        # ----------------------------------------------------
        # DÍA CERRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "closed_day":

            return jsonify({

                "success": False,

                "code": "closed_day",
                "reason": "closed_day",

                "error": "Ese día estamos cerrados."
            }), 400


        # ----------------------------------------------------
        # FECHA INVÁLIDA
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_date":

            return jsonify({

                "success": False,

                "code": "invalid_date",
                "reason": "invalid_date",

                "error": "La fecha seleccionada no es válida."
            }), 400


        # ----------------------------------------------------
        # CORRECTO
        # ----------------------------------------------------

        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue reprogramado correctamente."
        })


    except Exception as error:

        logger.exception("Error reprogramando turno")

        return jsonify({

            "success": False,

            "code": "INTERNAL_ERROR",
            "error": "No se pudo reprogramar el turno."
        }), 500


def api_reprogramar():
    from app import get_current_business_id
    return _get_public_reschedule_response(get_current_business_id())


def business_api_reprogramar(slug):
    from app import (
        get_current_business_id,
        resolve_business,
    )
    business = resolve_business(slug)
    if business is None:
        abort(404)

    g.current_business = business
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    return _get_public_reschedule_response(business_id)


# ============================================================
