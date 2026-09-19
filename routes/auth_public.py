"""
Módulo de rutas públicas de autenticación: forgot, reset, registro.

Contiene las view functions para:
- /forgot          -> endpoint: forgot_password
- /reset/<token>   -> endpoint: reset_password
- /registro        -> endpoint: registro

Estas rutas son públicas (no requieren autenticación ni tenant context).
"""

import re

from flask import (
    abort,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from extensions import valid_csrf_token
from services import platform as platform_service
from services import notifications
from database.database import (
    get_business_settings,
    get_business_settings_scoped,
)

from app import (
    _is_request_allowed,
    _rate_limit_key,
    get_client_ip,
    FORGOT_REQUEST_LIMIT,
    REGISTRO_REQUEST_LIMIT,
)


def forgot_password():
    """Formulario público para solicitar recuperación de contraseña."""
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return "Solicitud no válida", 400
        if not _is_request_allowed(_rate_limit_key("forgot", get_client_ip()), FORGOT_REQUEST_LIMIT):
            return redirect(url_for("forgot_password", sent="1"))
        email = (request.form.get("email", "") or "").strip().lower()
        if email:
            result = platform_service.request_password_reset(email)
            token = result.get("reset_token")
            if token:
                business = g.current_business
                business_name = (
                    business["name"] if business else "nuestro servicio"
                )
                reset_link = request.url_root.rstrip("/") + url_for("reset_password", token=token)
                notifications.send_password_reset_email(email, reset_link, business_name)
        return redirect(url_for("forgot_password", sent="1"))
    business = g.current_business
    business_settings = (
        get_business_settings_scoped(business["id"]) if business else get_business_settings()
    )
    business_name = (
        business_settings["business_name"] if business_settings and business_settings["business_name"]
        else "Mi negocio"
    )
    return render_template(
        "forgot.html",
        business_name=business_name,
        sent=request.args.get("sent") == "1",
    )


def registro():
    """Formulario público para solicitar el alta de un nuevo negocio.

    Un usuario anónimo puede solicitar el alta sin ser superadmin.
    El negocio se crea en estado pending (active=0, pending=1) y requiere
    aprobación manual del superadmin antes de activarse.
    """
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return "Solicitud no válida", 400
        if not _is_request_allowed(
            _rate_limit_key("registro", get_client_ip()), REGISTRO_REQUEST_LIMIT
        ):
            return redirect(url_for("registro", error=" Demasiadas solicitudes. Intentá nuevamente en unos minutos."))

        business_name = (request.form.get("business_name", "") or "").strip()
        slug = (request.form.get("slug", "") or "").strip().lower()
        owner_email = (request.form.get("owner_email", "") or "").strip().lower()

        if not business_name:
            return redirect(url_for("registro", error=" El nombre del negocio es obligatorio."))

        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", owner_email):
            return redirect(url_for("registro", error=" Ingresá un email válido."))

        try:
            result = platform_service.provision_business(business_name, slug or None, owner_email)
        except Exception:
            return redirect(url_for("registro", error=" No se pudo crear el negocio. Intentá con otro nombre, slug o email."))

        if not result["success"]:
            return redirect(url_for("registro", error=" No se pudo crear el negocio. Intentá con otro nombre o slug."))

        business_id = result["business_id"]
        notifications.send_apply_confirmation_email(owner_email, business_name)
        notifications.send_apply_notice_to_superadmin(business_name, business_id)
        return redirect(url_for("registro", sent="1"))

    error = request.args.get("error")
    sent = request.args.get("sent") == "1"
    settings = get_business_settings()
    business_name = settings["business_name"] if settings and settings["business_name"] else "TurnoBot"
    return render_template(
        "registro.html",
        business_name=business_name,
        sent=sent,
        error=error,
    )


def reset_password(token):
    """Establece una nueva contraseña usando el token de reset."""
    reset_token = platform_service.get_reset_token(token)
    if reset_token is None:
        return render_template("reset.html", invalid=True), 404

    if request.method == "GET":
        return render_template(
            "reset.html",
            invalid=False,
            email=reset_token["user_email"],
        )

    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400

    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    if password != password2:
        return render_template(
            "reset.html", invalid=False, email=reset_token["user_email"],
            error="Las contraseñas no coinciden.",
        ), 200

    result = platform_service.reset_password(token, password)
    if not result["success"]:
        if result["reason"] == "weak_password":
            error = "La contraseña debe tener al menos 12 caracteres."
        else:
            error = "Enlace no válido o vencido."
        return render_template("reset.html", invalid=False, email="", error=error), 200

    return redirect(url_for("login"))