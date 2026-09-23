"""
Módulo de rutas AUTH ADMIN: migración de admin routes desde app.py.

Este módulo NO define un Blueprint registrado. Las view functions se
registran vía app.add_url_rule() en routes/__init__.py preservando
los endpoint names globales (admin, admin_save_service, etc.).
"""

import math
import re
import secrets
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from application.requests import _human_reschedule_error
from application.security import _is_json_request, _json_error
from database.database import (
    create_resource_scoped,
    create_service_scoped,
    create_user_scoped,
    ensure_loyalty_settings_scoped,
    get_all_services_scoped,
    get_all_weekly_schedules_scoped,
    get_business_settings_scoped,
    get_connection,
    get_loyalty_account_by_id_scoped,
    get_membership_scoped,
    get_resource_scoped,
    get_resources_scoped,
    get_user_by_email_scoped,
    list_loyalty_accounts_scoped,
    list_points_ledger_scoped,
    recalculate_all_balances_scoped,
    set_resource_active_scoped,
    update_appointment_status_scoped,
    update_business_settings_scoped,
    update_loyalty_settings_scoped,
    update_service_scoped,
    update_weekly_schedule_scoped,
)
from extensions import valid_csrf_token
from routes.auth import _is_authenticated, _login_url, get_current_business_id
from services import loyalty, memberships, product
from services.appointments import (
    create_appointment,
    get_appointment_counts,
    get_appointments,
    reschedule_appointment_admin,
)
from services.conversations import (
    get_conversation_messages_scoped,
    get_conversation_session_by_id_scoped,
    get_conversation_stats_scoped,
    get_frequent_questions_scoped,
    get_opportunities_scoped,
    get_unanswered_questions_scoped,
    list_conversation_sessions_scoped,
    resolve_human_handoff_scoped,
    update_session_status_scoped,
)
from services.knowledge import (
    create_knowledge_scoped,
    delete_knowledge_scoped,
    get_knowledge_scoped,
    update_knowledge_scoped,
)
from services.notifications import smtp_configured

# ============================================================
# ADMIN HELPERS
# ============================================================


def _require_admin_membership():
    """Valida sesión + membresía administrativa (owner/admin) del negocio actual."""
    if not _is_authenticated():
        session.clear()
        return redirect(_login_url())
    user_id = session.get("user_id")
    business_id = get_current_business_id()
    membership = get_membership_scoped(user_id, business_id) if business_id else None
    if not membership or membership["role_name"] not in {"owner", "admin"}:
        session.clear()
        return redirect(_login_url())
    return None


def membership_required(*role_names):
    """Requiere que el usuario autenticado tenga una membresía con rol(es)
    permitido(s) en el negocio resuelto por el slug de la URL (nunca del cliente)."""
    allowed = set(role_names)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not _is_authenticated():
                session.clear()
                return redirect(_login_url())
            user_id = session.get("user_id")
            business_id = get_current_business_id()
            membership = get_membership_scoped(user_id, business_id) if business_id else None
            if not membership or membership["role_name"] not in allowed:
                session.clear()
                return redirect(_login_url())
            return f(*args, **kwargs)

        return decorated

    return decorator


def _service_form_data(form):
    name = form.get("name", "").strip()
    price_raw = form.get("price", "").strip()
    duration_raw = form.get("duration", "").strip()
    active = form.get("active") == "1"

    if not name:
        return None, "El nombre del servicio es obligatorio."

    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return None, "El precio debe ser numérico y mayor o igual a cero."

    if not math.isfinite(price) or price < 0:
        return None, "El precio debe ser numérico y mayor o igual a cero."

    try:
        duration = int(duration_raw)
    except (TypeError, ValueError):
        return None, "La duración debe ser un entero mayor que cero."

    if duration <= 0:
        return None, "La duración debe ser un entero mayor que cero."

    return (name, price, duration, active), None


def _business_identity(business_id):
    """Devuelve (business_name, business_initials) para las plantillas admin."""
    settings = get_business_settings_scoped(business_id)
    business_name = (
        settings["business_name"] if settings and settings["business_name"] else "Mi negocio"
    )
    business_initials = (
        settings["business_initials"] if settings and settings["business_initials"] else ""
    )
    return business_name, business_initials


def _admin_url(**kwargs):
    """Devuelve la URL del panel admin, con prefijo de slug si aplica."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_slug", slug=business["slug"], **kwargs)
    return url_for("admin", **kwargs)


def _usuarios_url(**kwargs):
    """Devuelve la URL de la página de usuarios, con prefijo de slug si aplica."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_usuarios", slug=business["slug"], **kwargs)
    return url_for("admin_usuarios", **kwargs)


def _fidelizacion_url(**kwargs):
    """URL de la página de fidelización, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_fidelizacion_slug", slug=business["slug"], **kwargs)
    return url_for("admin_fidelizacion", **kwargs)


def _resources_url(**kwargs):
    """URL de la página de recursos, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_recursos_slug", slug=business["slug"], **kwargs)
    return url_for("admin_recursos", **kwargs)


def _conocimiento_url(**kwargs):
    """URL de la página de conocimiento, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_conocimiento_slug", slug=business["slug"], **kwargs)
    return url_for("admin_conocimiento", **kwargs)


def _conversaciones_url(**kwargs):
    """URL de la página de conversaciones, con/sin prefijo de slug."""
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1:
        return url_for("admin_conversaciones_slug", slug=business["slug"], **kwargs)
    return url_for("admin_conversaciones", **kwargs)


def _human_appointment_error(reason):
    mapping = {
        "invalid_service": "El servicio indicado no es válido.",
        "invalid_time": "El horario indicado no es válido o ya no está disponible.",
        "occupied": "Ese horario ya está ocupado.",
        "invalid_date": "La fecha indicada no es válida.",
        "closed": "El negocio está cerrado en esa fecha.",
        "past": "No se puede reservar una fecha pasada.",
        "required_fields": "Faltan datos obligatorios.",
    }
    return mapping.get(reason, reason or "Operación no permitida.")


def _admin_usuarios_gate():
    """Autenticación administrativa para gestionar memberships.

    Solo valida sesión + membresía administrativa (owner/admin) del negocio
    actual. La política owner-only se delega a services.memberships para no
    duplicar reglas en la capa HTTP.
    """
    denied = _require_admin_membership()
    if denied:
        return denied
    return None


def _render_admin():
    business_id = get_current_business_id()
    settings = get_business_settings_scoped(business_id)
    if business_id is None:
        abort(404)
    if settings is None:
        settings = {
            "business_name": "Mi negocio",
            "business_initials": "",
            "business_type": "Negocio",
            "business_description": "",
            "timezone": "UTC",
            "slot_duration": 60,
            "break_between_slots": 0,
            "notifications_enabled": 0,
            "notification_email": "",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
        }
    services = get_all_services_scoped(business_id)
    onboarding = product.get_onboarding_state(business_id, settings, services)
    status = request.args.get("status") or "confirmed"
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        status = "confirmed"
    appointment_date = request.args.get("fecha") or None
    per_page = min(50, max(1, int(request.args.get("per_page", "25"))))
    total_appointments_for_status = get_appointment_counts(business_id).get(status, 0)
    total_pages = max(1, (total_appointments_for_status + per_page - 1) // per_page)
    page = max(1, min(int(request.args.get("page", "1")), total_pages))
    offset = (page - 1) * per_page
    actor_user_id = session.get("user_id")
    membership = (
        get_membership_scoped(actor_user_id, business_id) if actor_user_id and business_id else None
    )
    is_owner = bool(membership and membership["role_name"] == "owner")
    can_manage_memberships = bool(
        actor_user_id
        and business_id
        and memberships.can_manage_memberships(actor_user_id, business_id)
    )
    weekly_schedule = get_all_weekly_schedules_scoped(business_id) if business_id else []
    appointments_list = get_appointments(
        status=status,
        appointment_date=appointment_date,
        business_id=business_id,
        limit=per_page,
        offset=offset,
    )
    return render_template(
        "admin.html",
        appointments=appointments_list,
        counts=get_appointment_counts(business_id),
        selected_status=status,
        selected_date=appointment_date or "",
        business_name=settings["business_name"],
        business_initials=settings["business_initials"],
        business_settings=settings,
        weekly_schedule=weekly_schedule,
        config_message=request.args.get("config_message", ""),
        config_error=request.args.get("config_error", ""),
        services=services,
        resources=get_resources_scoped(business_id),
        resource_message=request.args.get("resource_message", ""),
        resource_error=request.args.get("resource_error", ""),
        onboarding=onboarding,
        product_summary=onboarding["summary"],
        service_message=request.args.get("service_message", ""),
        service_error=request.args.get("service_error", ""),
        reschedule_error=request.args.get("error_message", ""),
        schedule_message=request.args.get("schedule_message", ""),
        schedule_error=request.args.get("schedule_error", ""),
        can_manage_memberships=can_manage_memberships,
        is_owner=is_owner,
        smtp_configured=smtp_configured(),
        pagination={
            "page": page,
            "per_page": per_page,
            "total": total_appointments_for_status,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    )


# ============================================================
# ADMIN VIEW FUNCTIONS
# ============================================================


def admin():
    denied = _require_admin_membership()
    if denied:
        return denied
    return _render_admin()


def admin_slug(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)
    denied = _require_admin_membership()
    if denied:
        return denied
    return _render_admin()


def admin_save_service(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    values, error = _service_form_data(request.form)
    if error:
        return redirect(_admin_url(service_error=error))

    service_id = request.form.get("service_id", "").strip()
    try:
        if service_id:
            if not update_service_scoped(int(service_id), get_current_business_id(), *values):
                return redirect(_admin_url(service_error="No se encontró el servicio."))
        else:
            create_service_scoped(get_current_business_id(), *values)
    except (TypeError, ValueError):
        return redirect(_admin_url(service_error="El identificador del servicio no es válido."))

    return redirect(_admin_url(service_message="El servicio se guardó correctamente."))


def admin_toggle_service(service_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    active = request.form.get("active") == "1"
    business_id = get_current_business_id()
    service = next(
        (row for row in get_all_services_scoped(business_id) if row["id"] == service_id), None
    )
    if not service:
        return redirect(_admin_url(service_error="No se encontró el servicio."))

    update_service_scoped(
        service_id, business_id, service["name"], service["price"], service["duration"], active
    )
    return redirect(_admin_url(service_message="El estado del servicio se actualizó."))


def admin_update_business_settings(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    business_name = request.form.get("business_name", "").strip()
    business_type = request.form.get("business_type", "").strip()
    business_initials = request.form.get("business_initials", "").strip()
    business_description = request.form.get("business_description", "").strip()
    timezone = request.form.get("timezone", "").strip()
    notifications_enabled = request.form.get("notifications_enabled") == "1"
    notification_email = request.form.get("notification_email", "").strip()
    slot_duration_raw = request.form.get("slot_duration", "").strip()
    break_between_slots_raw = request.form.get("break_between_slots", "").strip()
    logo_url = request.form.get("logo_url", "").strip()
    primary_color = request.form.get("primary_color", "").strip()
    secondary_color = request.form.get("secondary_color", "").strip()

    if not business_name or not business_type or not business_initials:
        return redirect(_admin_url(config_error="Nombre, tipo e iniciales son obligatorios."))

    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return redirect(_admin_url(config_error="La zona horaria indicada no es válida."))

    slot_duration = None
    break_between_slots = None
    if slot_duration_raw:
        try:
            slot_duration = int(slot_duration_raw)
        except (TypeError, ValueError):
            return redirect(
                _admin_url(config_error="La duración de slot debe ser un número entero.")
            )
        if slot_duration < 1:
            return redirect(
                _admin_url(config_error="La duración de slot debe ser al menos 1 minuto.")
            )
    if break_between_slots_raw:
        try:
            break_between_slots = int(break_between_slots_raw)
        except (TypeError, ValueError):
            return redirect(
                _admin_url(config_error="El intervalo entre turnos debe ser un número entero.")
            )
        if break_between_slots < 0:
            return redirect(
                _admin_url(config_error="El intervalo entre turnos no puede ser negativo.")
            )

    _HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
    if primary_color and not _HEX_COLOR_RE.match(primary_color):
        return redirect(
            _admin_url(config_error="El color primario debe ser un HEX válido (#RRGGBB).")
        )
    if secondary_color and not _HEX_COLOR_RE.match(secondary_color):
        return redirect(
            _admin_url(config_error="El color secundario debe ser un HEX válido (#RRGGBB).")
        )
    if logo_url:
        logo_lower = logo_url.lower()
        if not (logo_lower.startswith("http://") or logo_lower.startswith("https://")):
            return redirect(
                _admin_url(config_error="El logo debe ser una URL válida (http/https).")
            )
        if " " in logo_url:
            return redirect(_admin_url(config_error="El logo no debe contener espacios."))

    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    update_business_settings_scoped(
        business_id,
        business_name,
        business_type,
        business_initials,
        business_description,
        timezone,
        notifications_enabled=notifications_enabled,
        notification_email=notification_email,
        slot_duration=slot_duration,
        break_between_slots=break_between_slots,
        logo_url=logo_url,
        primary_color=primary_color,
        secondary_color=secondary_color,
    )

    return redirect(
        _admin_url(config_message="La configuración del negocio se guardó correctamente.")
    )


def admin_save_weekly_schedule(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    for day in range(7):
        day_prefix = f"day_{day}"
        is_open = request.form.get(f"{day_prefix}_open") == "1"
        morning_start = request.form.get(f"{day_prefix}_morning_start", "").strip() or None
        morning_end = request.form.get(f"{day_prefix}_morning_end", "").strip() or None
        afternoon_start = request.form.get(f"{day_prefix}_afternoon_start", "").strip() or None
        afternoon_end = request.form.get(f"{day_prefix}_afternoon_end", "").strip() or None

        if not is_open:
            morning_start = None
            morning_end = None
            afternoon_start = None
            afternoon_end = None

        update_weekly_schedule_scoped(
            business_id, day, is_open, morning_start, morning_end, afternoon_start, afternoon_end
        )

    return redirect(
        _admin_url(schedule_message="Los horarios semanales se guardaron correctamente.")
    )


def admin_cancel_appointment(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        if _is_json_request():
            return _json_error("BAD_REQUEST", "Solicitud no válida"), 400
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    update_appointment_status_scoped(appointment_id, "cancelled", business_id)
    if _is_json_request():
        return jsonify({"success": True, "message": "Turno cancelado"})
    return redirect(_admin_url(status="confirmed"))


def admin_update_appointment_status(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        if _is_json_request():
            return _json_error("BAD_REQUEST", "Solicitud no válida"), 400
        return "Solicitud no válida", 400
    status = request.form.get("status", "")
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        if _is_json_request():
            return _json_error("BAD_REQUEST", "Estado no válido"), 400
        return "Estado no válido", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    update_appointment_status_scoped(appointment_id, status, business_id)
    if status == "completed":
        loyalty.award_points_for_completed(business_id, appointment_id)
    if _is_json_request():
        return jsonify({"success": True, "message": f"Estado actualizado a {status}"})
    return redirect(_admin_url(status=status))


def admin_reschedule_appointment(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        if _is_json_request():
            return _json_error("BAD_REQUEST", "Solicitud no válida"), 400
        return "Solicitud no válida", 400
    new_date = request.form.get("new_date", "").strip()
    new_time = request.form.get("new_time", "").strip()
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    error = None
    if not new_date or not new_time:
        error = "Completá la fecha y la hora para reprogramar."
    else:
        res = reschedule_appointment_admin(
            appointment_id, new_date, new_time, business_id=business_id
        )
        if not res["success"]:
            error = "No se pudo reprogramar el turno: " + _human_reschedule_error(res.get("reason"))

    if _is_json_request():
        if error:
            return _json_error("REPROGRAMAR_ERROR", error), 400
        return jsonify({"success": True, "message": "Turno reprogramado"})

    return redirect(_admin_url(status="confirmed", error_message=error))


def admin_reschedule_appointment_with_resource(appointment_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        if _is_json_request():
            return _json_error("BAD_REQUEST", "Solicitud no válida"), 400
        return "Solicitud no válida", 400
    new_date = request.form.get("new_date", "").strip()
    new_time = request.form.get("new_time", "").strip()
    resource_id_raw = request.form.get("resource_id", "").strip()
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    resource_id = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except (TypeError, ValueError):
            resource_id = None

    error = None
    if not new_date or not new_time:
        error = "Completá la fecha y la hora para reprogramar."
    else:
        res = reschedule_appointment_admin(
            appointment_id, new_date, new_time, business_id=business_id, resource_id=resource_id
        )
        if not res["success"]:
            error = "No se pudo reprogramar el turno: " + _human_reschedule_error(res.get("reason"))

    if _is_json_request():
        if error:
            return _json_error("REPROGRAMAR_ERROR", error), 400
        return jsonify({"success": True, "message": "Turno reprogramado"})

    return redirect(_admin_url(status="confirmed", error_message=error))


def admin_create_appointment_manual(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        if _is_json_request():
            return _json_error("BAD_REQUEST", "Solicitud no válida"), 400
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    customer_name = request.form.get("customer_name", "").strip()
    phone = request.form.get("phone", "").strip()
    service_name = request.form.get("service_name", "").strip()
    appointment_date = request.form.get("appointment_date", "").strip()
    appointment_time = request.form.get("appointment_time", "").strip()
    resource_id_raw = request.form.get("resource_id", "").strip()

    resource_id = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except (TypeError, ValueError):
            resource_id = None

    def _error(msg, code=400):
        if _is_json_request():
            return _json_error(code, msg), code
        return redirect(_admin_url(status="confirmed", error_message=msg))

    if not customer_name:
        return _error("El nombre del cliente es obligatorio.")
    if not phone:
        return _error("El teléfono del cliente es obligatorio.")
    if not service_name:
        return _error("El servicio es obligatorio.")
    if not appointment_date:
        return _error("La fecha es obligatoria.")
    if not appointment_time:
        return _error("El horario es obligatorio.")

    service = None
    for s in get_all_services_scoped(business_id):
        if s["name"] == service_name:
            service = s
            break
    if service is None:
        return _error("El servicio seleccionado no existe.")

    if resource_id is not None:
        resource = get_resource_scoped(resource_id, business_id)
        if resource is None:
            return _error("El recurso seleccionado no existe o no pertenece a este negocio.")
        if not resource["active"]:
            return _error("No podés reservar un recurso inactivo.")

    res = create_appointment(
        customer_name=customer_name,
        phone=phone,
        service=service_name,
        appointment_date=appointment_date,
        appointment_time=appointment_time,
        business_id=business_id,
        resource_id=resource_id,
    )
    if not res.get("success"):
        reason = res.get("reason") or "No se pudo crear la reserva."
        return _error("No se pudo crear la reserva: " + _human_appointment_error(reason))

    return jsonify(
        {
            "success": True,
            "message": f"Turno creado: {customer_name} - {appointment_date} {appointment_time}",
            "appointment_id": res.get("appointment_id"),
        }
    )


def admin_usuarios(slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    result = memberships.list_members(actor_user_id, business_id)
    if not result["success"]:
        return redirect(_admin_url(usuarios_error=result["reason"]))
    business_name, business_initials = _business_identity(business_id)
    membership = (
        get_membership_scoped(actor_user_id, business_id) if actor_user_id and business_id else None
    )
    is_owner = bool(membership and membership["role_name"] == "owner")
    return render_template(
        "usuarios.html",
        business_name=business_name,
        business_initials=business_initials,
        members=result["members"],
        current_user_id=actor_user_id,
        is_owner=is_owner,
        message=request.args.get("usuarios_message", ""),
        error=request.args.get("usuarios_error", ""),
    )


def admin_usuarios_invitar(slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    email = request.form.get("email", "").strip().lower()
    role_name = request.form.get("role_name", "").strip()

    if not email:
        return redirect(_usuarios_url(usuarios_error="El email es obligatorio."))

    user = get_user_by_email_scoped(email)
    if user is None:
        placeholder_hash = generate_password_hash(secrets.token_urlsafe(24))
        user_id = create_user_scoped(email, placeholder_hash, active=True)
        if user_id is None:
            return redirect(_usuarios_url(usuarios_error="No se pudo crear el usuario."))
        target_user_id = user_id
    else:
        target_user_id = user["id"]

    result = memberships.invite_member(actor_user_id, business_id, target_user_id, role_name)
    if not result["success"]:
        return redirect(_usuarios_url(usuarios_error=result["reason"]))
    return redirect(_usuarios_url(usuarios_message="Usuario agregado correctamente."))


def admin_usuarios_cambiar_rol(user_id, slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    role_name = request.form.get("role_name", "").strip()
    result = memberships.change_role(actor_user_id, business_id, user_id, role_name)
    if not result["success"]:
        return redirect(_usuarios_url(usuarios_error=result["reason"]))
    return redirect(_usuarios_url(usuarios_message="Rol actualizado correctamente."))


def admin_usuarios_revocar(user_id, slug=None):
    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    result = memberships.revoke_membership(actor_user_id, business_id, user_id)
    if not result["success"]:
        return redirect(_usuarios_url(usuarios_error=result["reason"]))
    return redirect(_usuarios_url(usuarios_message="Membresía revocada correctamente."))


# ============================================================
# FIDELIZACIÓN (MVP Etapa 10.1)
# ============================================================


def admin_fidelizacion(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    loyalty_settings = ensure_loyalty_settings_scoped(business_id)
    accounts = list_loyalty_accounts_scoped(business_id)
    selected_account_id = request.args.get("account_id", type=int)

    movements = []
    selected_account = None
    if selected_account_id is not None:
        selected_account = get_loyalty_account_by_id_scoped(selected_account_id, business_id)
        if selected_account is not None:
            movements = list_points_ledger_scoped(business_id, selected_account_id)

    business_name, business_initials = _business_identity(business_id)

    return render_template(
        "admin_fidelizacion.html",
        business_name=business_name,
        business_initials=business_initials,
        loyalty_settings=loyalty_settings,
        accounts=accounts,
        selected_account=selected_account,
        movements=movements,
        message=request.args.get("loyalty_message", ""),
        error=request.args.get("loyalty_error", ""),
        admin_prefix=("" if business_id == 1 else f"/b/{g.current_business['slug']}"),
    )


def admin_fidelizacion_configuracion(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    enabled = request.form.get("enabled") == "1"
    points_raw = request.form.get("points_per_completed_appointment", "").strip()
    try:
        points = int(points_raw)
    except (TypeError, ValueError):
        return redirect(
            _fidelizacion_url(loyalty_error="El valor de puntos debe ser un número entero.")
        )
    if points < 0:
        return redirect(
            _fidelizacion_url(
                loyalty_error="Los puntos por turno completado no pueden ser negativos."
            )
        )

    result = update_loyalty_settings_scoped(business_id, enabled, points)
    if not result:
        return redirect(
            _fidelizacion_url(loyalty_error="No se pudo guardar la configuración de fidelización.")
        )
    return redirect(
        _fidelizacion_url(loyalty_message="Configuración de fidelización guardada correctamente.")
    )


def admin_fidelizacion_ajustar(account_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    actor_user_id = session.get("user_id")

    delta_raw = request.form.get("delta", "").strip()
    reason = request.form.get("reason", "").strip()
    result = loyalty.adjust_points(business_id, account_id, delta_raw, reason, actor_user_id)
    if not result["success"]:
        return redirect(
            _fidelizacion_url(
                loyalty_error="Ajuste rechazado: " + str(result["reason"]), account_id=account_id
            )
        )
    return redirect(
        _fidelizacion_url(
            loyalty_message="Ajuste de puntos aplicado correctamente.", account_id=account_id
        )
    )


def admin_fidelizacion_recalcular(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    recalculate_all_balances_scoped(business_id)
    return redirect(_fidelizacion_url(loyalty_message="Saldo de todos los clientes recalculado."))


def admin_recompensas(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return "Solicitud no válida", 400
        result = loyalty.save_reward(
            business_id,
            request.form.get("reward_id"),
            request.form.get("name"),
            request.form.get("description"),
            request.form.get("points_cost"),
            request.form.get("active") == "1",
        )
        if not result["success"]:
            return redirect(_fidelizacion_url(loyalty_error="No se pudo guardar la recompensa."))
        return redirect(_fidelizacion_url(loyalty_message="Recompensa guardada."))
    connection = get_connection()
    try:
        redemptions = [
            dict(row)
            for row in connection.execute(
                """SELECT r.*, a.customer_name, a.customer_phone, w.name reward_name FROM redemptions r JOIN loyalty_accounts a ON a.id=r.account_id AND a.business_id=r.business_id JOIN rewards w ON w.id=r.reward_id AND w.business_id=r.business_id WHERE r.business_id=? ORDER BY r.id DESC""",
                (business_id,),
            ).fetchall()
        ]
    finally:
        connection.close()
    reward_id = request.args.get("edit", type=int)
    editing = next(
        (reward for reward in loyalty.list_rewards(business_id) if reward["id"] == reward_id), None
    )
    loyalty_enabled = bool(ensure_loyalty_settings_scoped(business_id).get("enabled"))
    return render_template(
        "admin_recompensas.html",
        rewards=loyalty.list_rewards(business_id),
        retention=loyalty.retention_candidates(business_id) if loyalty_enabled else [],
        redemptions=redemptions,
        editing=editing,
        loyalty_enabled=loyalty_enabled,
    )


def admin_recompensa_estado(reward_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    reward = next(
        (r for r in loyalty.list_rewards(get_current_business_id()) if r["id"] == reward_id), None
    )
    if reward is None:
        abort(404)
    loyalty.save_reward(
        get_current_business_id(),
        reward_id,
        reward["name"],
        reward["description"],
        reward["points_cost"],
        request.form.get("active") == "1",
    )
    return redirect(_fidelizacion_url())


# ============================================================
# RECURSOS ADMIN
# ============================================================


def admin_recursos(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    return _render_admin()


def admin_create_resource(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(_admin_url(resource_error="El nombre del recurso es obligatorio."))
    if len(name) > 120:
        return redirect(_admin_url(resource_error="El nombre del recurso es demasiado largo."))
    result = create_resource_scoped(business_id, name)
    if not result.get("success"):
        if result.get("reason") == "duplicate_name":
            return redirect(
                _admin_url(resource_error="Ya existe un recurso con ese nombre en este negocio.")
            )
        return redirect(_admin_url(resource_error="No se pudo crear el recurso."))
    return redirect(_admin_url(resource_message="El recurso se creó correctamente."))


def admin_toggle_resource(resource_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    resource = get_resource_scoped(resource_id, business_id)
    if resource is None:
        return redirect(_admin_url(resource_error="No se encontró el recurso."))
    new_active = request.form.get("active") == "1"
    set_resource_active_scoped(resource_id, business_id, new_active)
    return redirect(_admin_url(resource_message="El estado del recurso se actualizó."))


# ============================================================
# CONOCIMIENTO ADMIN
# ============================================================


def admin_conocimiento(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    knowledge = get_knowledge_scoped(business_id)
    business_name, business_initials = _business_identity(business_id)
    return render_template(
        "admin_conocimiento.html",
        business_name=business_name,
        business_initials=business_initials,
        knowledge=knowledge,
        message=request.args.get("conocimiento_message", ""),
        error=request.args.get("conocimiento_error", ""),
    )


def admin_conocimiento_crear(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    type_ = request.form.get("type", "").strip()
    question = request.form.get("question", "").strip()
    answer = request.form.get("answer", "").strip()
    tags = request.form.get("tags", "").strip()
    if not type_ or not question or not answer:
        return redirect(
            _conocimiento_url(conocimiento_error="Tipo, pregunta y respuesta son obligatorios.")
        )
    actor_user_id = session.get("user_id")
    create_knowledge_scoped(business_id, type_, question, answer, tags, actor_user_id)
    return redirect(
        _conocimiento_url(conocimiento_message="Entrada de conocimiento creada correctamente.")
    )


def admin_conocimiento_editar(knowledge_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    type_ = request.form.get("type", "").strip()
    question = request.form.get("question", "").strip()
    answer = request.form.get("answer", "").strip()
    tags = request.form.get("tags", "").strip()
    active = request.form.get("active") == "1"
    if not type_ or not question or not answer:
        return redirect(
            _conocimiento_url(conocimiento_error="Tipo, pregunta y respuesta son obligatorios.")
        )
    if not update_knowledge_scoped(
        knowledge_id, business_id, type_, question, answer, tags, active
    ):
        return redirect(_conocimiento_url(conocimiento_error="No se pudo actualizar la entrada."))
    return redirect(
        _conocimiento_url(conocimiento_message="Entrada de conocimiento actualizada correctamente.")
    )


def admin_conocimiento_eliminar(knowledge_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)
    if not delete_knowledge_scoped(knowledge_id, business_id):
        return redirect(_conocimiento_url(conocimiento_error="No se pudo eliminar la entrada."))
    return redirect(
        _conocimiento_url(conocimiento_message="Entrada de conocimiento eliminada correctamente.")
    )


# ============================================================
# CONVERSACIONES ADMIN
# ============================================================


def admin_conversaciones(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    status_filter = request.args.get("status", "all")
    if status_filter not in ("all", "active", "needs_human", "human_resolved", "closed"):
        status_filter = "all"

    status_filter_db = None if status_filter == "all" else status_filter
    conversations = list_conversation_sessions_scoped(
        business_id, status=status_filter_db, limit=100
    )

    business_name, business_initials = _business_identity(business_id)

    stats = get_conversation_stats_scoped(business_id)

    return render_template(
        "admin_conversaciones.html",
        business_name=business_name,
        business_initials=business_initials,
        conversations=conversations,
        stats=stats,
        status_filter=status_filter,
        message=request.args.get("conversaciones_message", ""),
        error=request.args.get("conversaciones_error", ""),
    )


def admin_conversaciones_detalle(session_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    session = get_conversation_session_by_id_scoped(session_id, business_id)
    if not session:
        return redirect(_conversaciones_url(conversaciones_error="Conversación no encontrada."))

    messages = get_conversation_messages_scoped(session_id, business_id)

    business_name, business_initials = _business_identity(business_id)

    return render_template(
        "admin_conversaciones_detalle.html",
        business_name=business_name,
        business_initials=business_initials,
        session=session,
        messages=messages,
        message=request.args.get("conversaciones_message", ""),
        error=request.args.get("conversaciones_error", ""),
    )


def admin_conversaciones_resolver(session_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    if not resolve_human_handoff_scoped(session_id, business_id):
        return redirect(
            _conversaciones_url(conversaciones_error="No se pudo resolver la conversación.")
        )

    return redirect(
        _conversaciones_url(conversaciones_message="Conversación marcada como resuelta.")
    )


def admin_conversaciones_estado(session_id, slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    new_status = request.form.get("status", "").strip()
    if new_status not in ("active", "needs_human", "human_resolved", "closed"):
        return redirect(_conversaciones_url(conversaciones_error="Estado inválido."))

    if not update_session_status_scoped(session_id, business_id, new_status):
        return redirect(
            _conversaciones_url(conversaciones_error="No se pudo actualizar el estado.")
        )

    return redirect(_conversaciones_url(conversaciones_message="Estado actualizado correctamente."))


# ============================================================
# ANALYTICS: INTELIGENCIA
# ============================================================


def admin_inteligencia(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    frequent_questions = get_frequent_questions_scoped(business_id, limit=50)
    unanswered_questions = get_unanswered_questions_scoped(business_id, limit=50)
    opportunities = get_opportunities_scoped(business_id, limit=20)
    stats = get_conversation_stats_scoped(business_id)

    business_name, business_initials = _business_identity(business_id)

    return render_template(
        "admin_inteligencia.html",
        business_name=business_name,
        business_initials=business_initials,
        frequent_questions=frequent_questions,
        unanswered_questions=unanswered_questions,
        opportunities=opportunities,
        stats=stats,
        message=request.args.get("inteligencia_message", ""),
        error=request.args.get("inteligencia_error", ""),
    )


def admin_inteligencia_oportunidades(slug=None):
    denied = _require_admin_membership()
    if denied:
        return denied
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    opportunities = get_opportunities_scoped(business_id, limit=50)
    stats = get_conversation_stats_scoped(business_id)

    business_name, business_initials = _business_identity(business_id)

    return render_template(
        "admin_inteligencia_oportunidades.html",
        business_name=business_name,
        business_initials=business_initials,
        opportunities=opportunities,
        stats=stats,
        message=request.args.get("inteligencia_message", ""),
        error=request.args.get("inteligencia_error", ""),
    )
