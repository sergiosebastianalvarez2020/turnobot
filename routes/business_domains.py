"""Owner-only management of custom business domains."""

import sqlite3

from flask import abort, current_app, g, render_template, request, session

from application.business_domains import (
    DOMAIN_VERIFICATION_TTL,
    DomainVerificationError,
    InvalidHostname,
    check_dns_txt_verification,
    check_public_tls,
    new_verification_token,
    normalize_hostname,
    verification_record_name,
)
from application.requests import get_client_ip
from database.business_domains import (
    create_business_domain,
    get_business_domain,
    list_business_domains,
    revoke_business_domain,
    rotate_business_domain_token,
    set_business_domain_active,
    set_business_domain_tls_status,
    set_primary_business_domain,
    verify_business_domain,
)
from database.database import get_membership_scoped, get_user_by_id_scoped
from extensions import _is_request_allowed, valid_csrf_token
from routes.admin import _require_admin_membership


def _authorize_owner():
    denied = _require_admin_membership()
    if denied:
        return denied
    business = g.current_business
    user_id = session.get("user_id")
    membership = get_membership_scoped(user_id, business["id"]) if business and user_id else None
    if not membership or membership["role_name"] != "owner":
        abort(403)
    return None


def _authorize_slug(slug):
    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)
    return _authorize_owner()


def _audit_identity():
    user = session.get("user_id")
    actor = get_user_by_id_scoped(user) if user else None
    return (actor["email"] if actor else ""), get_client_ip()


def _allow_domain_action():
    ip_address = get_client_ip()
    business_id = g.current_business["id"]
    allowed = _is_request_allowed(f"custom-domain:{ip_address}:business:{business_id}", 5)
    return allowed


def _render_domains(*, error="", message="", verification=None):
    business = g.current_business
    return render_template(
        "business_domains.html",
        domains=list_business_domains(business["id"]),
        business=business,
        error=error,
        message=message,
        verification=verification,
        verification_record_name=verification_record_name,
        verification_ttl=DOMAIN_VERIFICATION_TTL,
        public_base_url=current_app.config["PUBLIC_BASE_URL"],
        admin_prefix=("" if business["id"] == 1 else f"/b/{business['slug']}"),
    )


def business_domains(slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    return _render_domains(
        error=request.args.get("error", ""), message=request.args.get("message", "")
    )


def add_business_domain(slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    try:
        hostname = normalize_hostname(request.form.get("hostname", ""))
    except InvalidHostname as error:
        return _render_domains(error=str(error)), 400
    if hostname in current_app.config.get("PLATFORM_HOSTNAMES", ()):
        return _render_domains(error="Ese hostname está reservado para la plataforma."), 400

    token, token_hash = new_verification_token()
    actor_email, ip_address = _audit_identity()
    try:
        create_business_domain(
            g.current_business["id"], hostname, token_hash, actor_email, ip_address
        )
    except sqlite3.IntegrityError:
        return _render_domains(error="Ese dominio ya está registrado en TurnoBot."), 409
    return _render_domains(
        message="Dominio agregado. Configurá el registro TXT indicado y luego verificá.",
        verification={"hostname": hostname, "token": token},
    )


def rotate_business_domain_verification(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    if not _allow_domain_action():
        return _render_domains(error="Demasiados intentos. Esperá unos minutos."), 429
    token, token_hash = new_verification_token()
    actor_email, ip_address = _audit_identity()
    hostname = rotate_business_domain_token(
        g.current_business["id"], domain_id, token_hash, actor_email, ip_address
    )
    if not hostname:
        abort(404)
    return _render_domains(
        message="Se rotó el token. El dominio quedó pendiente de verificación.",
        verification={"hostname": hostname, "token": token},
    )


def verify_business_domain_dns(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    if not _allow_domain_action():
        return _render_domains(error="Demasiados intentos. Esperá unos minutos."), 429

    domain = get_business_domain(g.current_business["id"], domain_id)
    if not domain:
        abort(404)
    if domain["verification_status"] != "pending" or not domain["verification_token_hash"]:
        return _render_domains(error="El dominio no tiene una verificación pendiente."), 400
    try:
        check_dns_txt_verification(domain["hostname"], domain["verification_token_hash"])
    except DomainVerificationError as error:
        return _render_domains(error=str(error)), 400

    actor_email, ip_address = _audit_identity()
    if not verify_business_domain(
        g.current_business["id"],
        domain_id,
        domain["verification_token_hash"],
        actor_email,
        ip_address,
    ):
        return _render_domains(
            error="El token venció o cambió durante la verificación. Rotá el token e intentá otra vez."
        ), 409
    return _render_domains(
        message="Propiedad verificada. Configurá HTTPS y ejecutá la comprobación TLS."
    )


def verify_business_domain_tls(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    if not _allow_domain_action():
        return _render_domains(error="Demasiados intentos. Esperá unos minutos."), 429

    domain = get_business_domain(g.current_business["id"], domain_id)
    if not domain:
        abort(404)
    if domain["verification_status"] != "verified":
        return _render_domains(error="Primero verificá la propiedad del dominio."), 400
    try:
        check_public_tls(domain["hostname"])
    except DomainVerificationError as error:
        actor_email, ip_address = _audit_identity()
        set_business_domain_tls_status(
            g.current_business["id"], domain_id, "failed", actor_email, ip_address
        )
        return _render_domains(error=str(error)), 400

    actor_email, ip_address = _audit_identity()
    set_business_domain_tls_status(
        g.current_business["id"], domain_id, "verified", actor_email, ip_address
    )
    return _render_domains(message="Certificado HTTPS validado. Ya podés activar el dominio.")


def activate_business_domain(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    actor_email, ip_address = _audit_identity()
    if not set_business_domain_active(
        g.current_business["id"], domain_id, True, actor_email, ip_address
    ):
        return _render_domains(
            error="Solo se pueden activar dominios verificados con HTTPS validado."
        ), 400
    return _render_domains(message="Dominio activado.")


def deactivate_business_domain(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    actor_email, ip_address = _audit_identity()
    if not set_business_domain_active(
        g.current_business["id"], domain_id, False, actor_email, ip_address
    ):
        abort(404)
    return _render_domains(message="Dominio desactivado.")


def revoke_business_domain_route(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    actor_email, ip_address = _audit_identity()
    if not revoke_business_domain(g.current_business["id"], domain_id, actor_email, ip_address):
        abort(404)
    return _render_domains(message="Dominio revocado y desconectado.")


def set_primary_business_domain_route(domain_id, slug=None):
    denied = _authorize_slug(slug) if slug else _authorize_owner()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    actor_email, ip_address = _audit_identity()
    if not set_primary_business_domain(
        g.current_business["id"], domain_id, actor_email, ip_address
    ):
        return _render_domains(
            error="El dominio principal debe estar verificado, activo y tener HTTPS válido."
        ), 400
    return _render_domains(message="Dominio principal actualizado.")


def register(app):
    rules = (
        ("/admin/dominios", "business_domains", business_domains, ("GET",)),
        ("/b/<slug>/admin/dominios", "business_domains_slug", business_domains, ("GET",)),
        ("/admin/dominios/agregar", "add_business_domain", add_business_domain, ("POST",)),
        (
            "/b/<slug>/admin/dominios/agregar",
            "add_business_domain_slug",
            add_business_domain,
            ("POST",),
        ),
    )
    for rule, endpoint, view, methods in rules:
        app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=list(methods))

    actions = (
        ("verificar", verify_business_domain_dns),
        ("tls", verify_business_domain_tls),
        ("activar", activate_business_domain),
        ("desactivar", deactivate_business_domain),
        ("revocar", revoke_business_domain_route),
        ("principal", set_primary_business_domain_route),
        ("rotar-token", rotate_business_domain_verification),
    )
    for action, view in actions:
        for prefix, endpoint_suffix in (("", ""), ("/b/<slug>", "_slug")):
            app.add_url_rule(
                f"{prefix}/admin/dominios/<int:domain_id>/{action}",
                endpoint=f"business_domain_{action.replace('-', '_')}{endpoint_suffix}",
                view_func=view,
                methods=["POST"],
            )
