"""
Módulo de rutas de invitaciones: staff, public, admin panel.

Contiene las view functions para:
- /b/<slug>/invitacion-staff/<token>      -> endpoint: staff_invitation
- /b/<slug>/invitacion/<token>            -> endpoint: public_invitation
- /admin/usuarios/invitar-enlace          -> endpoint: admin_usuarios_invitar_enlace
- /b/<slug>/admin/usuarios/invitar-enlace -> endpoint: admin_usuarios_invitar_enlace

Estas rutas manejan invitaciones para staff/admin y owner (público).
"""

from flask import abort, g, redirect, render_template, request, session, url_for

from extensions import valid_csrf_token
from services import platform as platform_service
from services.notifications import send_staff_invitation_email


def staff_invitation(slug, token):
    """Página pública donde el staff/admin acepta la invitación."""
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)

    invitation = platform_service.get_staff_invitation_for_business(business["id"], token)
    if invitation is None:
        return render_template(
            "staff_invitation.html", business_name=business["name"], invalid=True
        ), 404

    if request.method == "GET":
        return render_template(
            "staff_invitation.html",
            business_name=business["name"],
            invitation=invitation,
            error=None,
            invalid=False,
        )

    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    if password != password2:
        return render_template(
            "staff_invitation.html",
            business_name=business["name"],
            invitation=invitation,
            error="Las contraseñas no coinciden.",
            invalid=False,
        ), 200

    result = platform_service.accept_staff_invitation(business["id"], token, password)
    if not result["success"]:
        if result["reason"] == "weak_password":
            error = "La contraseña debe tener al menos 12 caracteres."
        else:
            error = "Enlace no válido o vencido."
        return render_template(
            "staff_invitation.html",
            business_name=business["name"],
            invitation=invitation,
            error=error,
            invalid=False,
        ), 200

    return redirect(url_for("login_slug", slug=business["slug"]))


def public_invitation(slug, token):
    """Página pública donde el owner establece su contraseña."""
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)
    business_id = business["id"]
    invitation = platform_service.get_invitation_for_business(business_id, token)
    if invitation is None:
        return render_template("invitacion.html", business=business, invalid=True), 404

    if request.method == "GET":
        return render_template(
            "invitacion.html", business=business, invitation=invitation, error=None, invalid=False
        )

    # POST: validar token antes que CSRF para que token vencido/usado devuelva 404
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")

    if password != password2:
        return render_template(
            "invitacion.html",
            business=business,
            invitation=invitation,
            error="Las contraseñas no coinciden.",
        ), 200

    result = platform_service.accept_invitation(business_id, token, password)
    if not result["success"]:
        if result["reason"] == "weak_password":
            error = "La contraseña debe tener al menos 12 caracteres."
        elif result["reason"] == "invalid_token":
            error = "Enlace no válido o vencido."
        else:
            error = "No se pudo aceptar la invitación."
        return render_template(
            "invitacion.html", business=business, invitation=invitation, error=error
        ), 200

    return redirect(f"/b/{business['slug']}/login")


def admin_usuarios_invitar_enlace(slug=None):
    """El owner invita a un staff/admin por email (invitación con link)."""
    from routes.admin import _admin_usuarios_gate, _usuarios_url
    from routes.auth import get_current_business_id

    denied = _admin_usuarios_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    actor_user_id = session.get("user_id")
    business_id = get_current_business_id()
    if business_id is None:
        abort(404)

    email = (request.form.get("email", "") or "").strip().lower()
    role_name = request.form.get("role_name", "").strip()

    if not email:
        return redirect(_usuarios_url(usuarios_error="El email es obligatorio."))
    if role_name not in ("admin", "staff"):
        return redirect(_usuarios_url(usuarios_error="Rol no permitido."))

    result = platform_service.create_staff_invitation(
        business_id, email, role_name, actor_user_id=actor_user_id
    )
    if not result["success"]:
        if result["reason"] == "forbidden":
            return redirect(_usuarios_url(usuarios_error="Solo el owner puede invitar staff."))
        if result["reason"] == "cannot_invite_yourself":
            return redirect(_usuarios_url(usuarios_error="No podés invitarte a vos mismo."))
        return redirect(_usuarios_url(usuarios_error="No se pudo crear la invitación."))

    token = result["invitation_token"]
    business = platform_service.get_business_by_id_platform(business_id)
    business_name = business["name"] if business else "tu negocio"
    invitation_link = request.url_root.rstrip("/") + url_for(
        "staff_invitation", slug=g.current_business["slug"], token=token
    )
    sent, reason = send_staff_invitation_email(email, invitation_link, business_name, role_name)
    if not sent and reason != "disabled":
        return redirect(_usuarios_url(usuarios_error="No se pudo enviar el email de invitación."))
    return redirect(_usuarios_url(usuarios_message="Invitación enviada a " + email + "."))
