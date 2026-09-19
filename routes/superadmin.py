"""
Módulo de rutas de SUPERADMIN (plataforma).

Contiene las view functions para la administración de la plataforma:
- /superadmin/login           -> endpoint: superadmin_login
- /superadmin/logout          -> endpoint: superadmin_logout
- /superadmin                 -> endpoint: superadmin_panel
- /superadmin/auditoria       -> endpoint: superadmin_audit
- /superadmin/negocios/crear  -> endpoint: superadmin_negocios_crear
- /superadmin/negocios/<id>   -> endpoint: superadmin_negocios_detalle
- /superadmin/negocios/<id>/aprobar     -> endpoint: superadmin_negocios_aprobar
- /superadmin/negocios/<id>/desactivar  -> endpoint: superadmin_negocios_desactivar
- /superadmin/negocios/<id>/activar     -> endpoint: superadmin_negocios_activar
- /superadmin/negocios/<id>/reinviar    -> endpoint: superadmin_negocios_reinviar

Estas rutas operan a nivel de PLATAFORMA (superadmin), no son tenant-scoped.
"""

import secrets

from flask import (
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from extensions import valid_csrf_token
from services import platform as platform_service
from services.notifications import (
    send_apply_confirmation_email,
    send_apply_notice_to_superadmin,
)
from database.database import (
    get_business_settings_scoped,
    list_members_scoped,
    revoke_all_platform_sessions,
)


def _superadmin_gate():
    """Gate de autenticación SUPERADMIN: redirige a login si no hay sesión de plataforma."""
    from app import _is_platform_authenticated
    if not _is_platform_authenticated():
        return redirect("/superadmin/login")
    return None


def superadmin_login():
    from app import _platform_session_expires_at, _platform_current_user, get_client_ip
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template("superadmin_login.html", error=True, error_message="Solicitud no válida"), 400
        email = (request.form.get("email", "") or "").strip().lower()
        password = request.form.get("password", "") or ""
        platform_user, error = platform_service.authenticate_superadmin(email, password)
        if platform_user is None:
            return render_template("superadmin_login.html", error=True, error_message=error or "Credenciales inválidas."), 200
        token = secrets.token_urlsafe(48)
        expires_at = _platform_session_expires_at()
        platform_service.establish_platform_session(platform_user["id"], token, expires_at)
        session["platform_user_id"] = platform_user["id"]
        session["platform_session_token"] = token
        platform_service.log_platform_action(platform_user["id"], platform_user["email"], None, "superadmin_login", "", get_client_ip())
        return redirect("/superadmin")

    user, _ = _platform_current_user()
    if user:
        return redirect("/superadmin")
    return render_template("superadmin_login.html", error=False)


def superadmin_logout():
    from app import _platform_current_user, _clear_platform_session, get_client_ip
    user, token = _platform_current_user()
    if user is None:
        return redirect("/superadmin/login")
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    revoke_all_platform_sessions(user["id"])
    _clear_platform_session()
    platform_service.log_platform_action(user["id"], user["email"], None, "superadmin_logout", "", get_client_ip())
    return redirect("/superadmin/login")


def superadmin_panel():
    from app import _platform_current_user
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    message = request.args.get("message", "")
    error = request.args.get("error", "")
    businesses = platform_service.list_businesses_for_platform()
    return render_template(
        "superadmin.html",
        superadmin=user,
        businesses=businesses,
        section="businesses",
        message=message,
        error=error,
        platform_lifetime=platform_service.invitation_lifetime_hours(),
    )


def superadmin_audit():
    from app import _platform_current_user
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    audit_rows = platform_service.list_audit_log()
    return render_template(
        "superadmin.html",
        superadmin=user,
        section="audit",
        audit_rows=audit_rows,
    )


def superadmin_negocios_crear():
    from app import _platform_current_user, get_client_ip
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    name = (request.form.get("nombre", "") or "").strip()
    slug = (request.form.get("slug", "") or "").strip().lower()
    owner_email = (request.form.get("owner_email", "") or "").strip().lower()
    user, _ = _platform_current_user()
    result = platform_service.provision_business(name, slug, owner_email)
    if not result["success"]:
        return redirect(f"/superadmin?error={result['reason']}")
    platform_service.log_platform_action(
        user["id"], user["email"], result["business_id"],
        "business_created", f"Negocio creado: {name} (slug: {result['slug']})",
        get_client_ip()
    )
    # EMAIL 1: confirmación al solicitante + aviso al superadmin
    send_apply_confirmation_email(owner_email, name)
    send_apply_notice_to_superadmin(name, result["business_id"])
    return redirect("/superadmin?message=Solicitud de alta registrada correctamente.")


def superadmin_negocios_detalle(business_id):
    from app import _platform_current_user
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    business = platform_service.get_business_by_id_platform(business_id)
    if business is None:
        abort(404)
    business_settings = get_business_settings_scoped(business_id)
    if business_settings:
        business_settings = dict(business_settings)
    members = [dict(m) for m in list_members_scoped(business_id)]
    invitations = [dict(i) for i in platform_service.list_invitations(business_id)]
    message = request.args.get("message", "")
    error = request.args.get("error", "")
    return render_template(
        "superadmin.html",
        superadmin=user,
        section="business_detail",
        business=business,
        business_settings=business_settings,
        members=members,
        invitations=invitations,
        message=message,
        error=error,
    )


def superadmin_negocios_aprobar(business_id):
    from app import _platform_current_user, get_client_ip, send_approved_invitation_email
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    result = platform_service.approve_business(business_id)
    if not result["success"]:
        return redirect(f"/superadmin/negocios/{business_id}?error={result['reason']}")
    platform_service.log_platform_action(
        user["id"], user["email"], business_id,
        "business_approved", f"Negocio aprobado: {result['slug']} (slug: {result['slug']})",
        get_client_ip()
    )
    # EMAIL 2: invitación al owner para definir contraseña
    send_approved_invitation_email(
        result["owner_email"],
        result["slug"],
        request.url_root.rstrip("/") + result["invitation_url"],
        result.get("expires_at", ""),
        platform_service.invitation_lifetime_hours(),
    )
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio aprobado e invitación generada.")


def superadmin_negocios_desactivar(business_id):
    from app import _platform_current_user, get_client_ip
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ok = platform_service.set_business_active(business_id, False)
    if not ok:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se pudo suspender el negocio.")
    platform_service.log_platform_action(user["id"], user["email"], business_id, "business_disabled", "Negocio suspendido", get_client_ip())
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio suspendido correctamente.")


def superadmin_negocios_activar(business_id):
    from app import _platform_current_user, get_client_ip
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ok = platform_service.set_business_active(business_id, True)
    if not ok:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se pudo activar el negocio.")
    platform_service.log_platform_action(user["id"], user["email"], business_id, "business_enabled", "Negocio reactivado", get_client_ip())
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio activado correctamente.")


def superadmin_negocios_reinviar(business_id):
    from app import _platform_current_user, get_client_ip, send_approved_invitation_email
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    # buscar owner del negocio
    owner = None
    for m in [dict(m) for m in list_members_scoped(business_id)]:
        if m.get("role_name") == "owner":
            owner = m
            break
    if owner is None:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se encontró el owner del negocio.")
    token, expires_at = platform_service.resend_invitation(business_id, owner["user_id"], owner["email"])
    platform_service.log_platform_action(user["id"], user["email"], business_id, "invitation_resent", f"Nueva invitación enviada a {owner['email']}", get_client_ip())
    # EMAIL 2: nueva invitación al owner
    business = platform_service.get_business_by_id_platform(business_id)
    if business:
        send_approved_invitation_email(
            owner["email"],
            business["name"],
            request.url_root.rstrip("/") + f"/b/{business['slug']}/invitacion/{token}",
            expires_at,
            platform_service.invitation_lifetime_hours(),
        )
    return redirect(f"/superadmin/negocios/{business_id}?message=Nueva invitación generada.")