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
- /superadmin/identidad                 -> endpoint: superadmin_visual_identity
- /superadmin/identidad/<id>            -> endpoint: superadmin_visual_identity_detail
- /superadmin/identidad/<id>/guardar    -> endpoint: superadmin_visual_identity_save
- /superadmin/identidad/<id>/bloqueos   -> endpoint: superadmin_visual_identity_locks
- /superadmin/identidad/<id>/preset     -> endpoint: superadmin_visual_identity_preset
- /superadmin/ia                        -> endpoint: superadmin_ia
- /superadmin/ia/guardar                -> endpoint: superadmin_ia_guardar
- /superadmin/ia/<id>/activar           -> endpoint: superadmin_ia_activar
- /superadmin/ia/<id>/desactivar        -> endpoint: superadmin_ia_desactivar
- /superadmin/ia/<id>/prioridad         -> endpoint: superadmin_ia_prioridad
- /superadmin/ia/<id>/eliminar          -> endpoint: superadmin_ia_eliminar
- /superadmin/ia/<id>/probar            -> endpoint: superadmin_ia_probar

Estas rutas operan a nivel de PLATAFORMA (superadmin), no son tenant-scoped.
"""

import logging
import secrets
from urllib.parse import quote

from flask import abort, redirect, render_template, request, session

from application.platform import (
    _clear_platform_session,
    _is_platform_authenticated,
    _platform_current_user,
    _platform_session_expires_at,
)
from application.public_urls import public_url
from application.rate_limit import is_api_request_allowed, is_login_request_allowed
from application.requests import get_client_ip
from application.theme_colors import contrast_pairs, palette_summary
from application.themes import (
    ALLOWED_FONTS,
    COLOR_KEYS,
    CONFIG_KEYS,
    DEFAULT_PRESET,
    LOCKABLE_OPTIONS,
    MEDIA_KEYS,
    preset_catalog,
    preset_names,
)
from database.database import (
    get_business_settings_scoped,
    list_members_scoped,
    revoke_all_platform_sessions,
)
from extensions import valid_csrf_token
from services import platform as platform_service
from services import visual_identity as visual_identity_service
from services.ai_config import (
    AIConfigError,
    create_ai_provider,
    delete_ai_provider,
    get_ai_provider,
    get_decrypted_api_key,
    list_ai_providers,
    set_ai_provider_active,
    set_ai_provider_priority,
    update_ai_provider,
)
from services.ai_encryption import AIEncryptionError
from services.ai_encryption import is_configured as encryption_is_configured
from services.ai_providers.gemini import GeminiProvider
from services.ai_providers.groq import CerebrasProvider, GroqProvider, OpenRouterProvider
from services.notifications import send_apply_confirmation_email, send_apply_notice_to_superadmin


def _superadmin_gate():
    """Gate de autenticación SUPERADMIN: redirige a login si no hay sesión de plataforma."""
    if not _is_platform_authenticated():
        return redirect("/superadmin/login")
    return None


def _check_ia_rate_limit(client_ip: str, endpoint: str) -> bool:
    """Verifica el rate limiting para rutas administrativas de IA.

    Usa un límite separado del login para evitar acoplamiento innecesario.
    """
    return is_api_request_allowed(client_ip, endpoint=endpoint)


def superadmin_login():
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            return render_template(
                "superadmin_login.html", error=True, error_message="Solicitud no válida"
            ), 400
        if not is_login_request_allowed(get_client_ip()):
            return render_template(
                "superadmin_login.html",
                error=True,
                error_message="Demasiados intentos. Esperá unos minutos.",
            ), 429
        email = (request.form.get("email", "") or "").strip().lower()
        password = request.form.get("password", "") or ""
        platform_user, error = platform_service.authenticate_superadmin(email, password)
        if platform_user is None:
            return render_template(
                "superadmin_login.html",
                error=True,
                error_message=error or "Credenciales inválidas.",
            ), 200
        token = secrets.token_urlsafe(48)
        expires_at = _platform_session_expires_at()
        platform_service.establish_platform_session(platform_user["id"], token, expires_at)
        session["platform_user_id"] = platform_user["id"]
        session["platform_session_token"] = token
        platform_service.log_platform_action(
            platform_user["id"],
            platform_user["email"],
            None,
            "superadmin_login",
            "",
            get_client_ip(),
        )
        return redirect("/superadmin")

    user, _ = _platform_current_user()
    if user:
        return redirect("/superadmin")
    return render_template("superadmin_login.html", error=False)


def superadmin_logout():
    user, token = _platform_current_user()
    if user is None:
        return redirect("/superadmin/login")
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    revoke_all_platform_sessions(user["id"])
    _clear_platform_session()
    platform_service.log_platform_action(
        user["id"], user["email"], None, "superadmin_logout", "", get_client_ip()
    )
    return redirect("/superadmin/login")


def superadmin_panel():
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
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    audit_rows = platform_service.list_audit_log()
    return render_template(
        "superadmin.html", superadmin=user, section="audit", audit_rows=audit_rows
    )


def superadmin_negocios_crear():
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
        user["id"],
        user["email"],
        result["business_id"],
        "business_created",
        f"Negocio creado: {name} (slug: {result['slug']})",
        get_client_ip(),
    )
    # EMAIL 1: confirmación al solicitante + aviso al superadmin
    send_apply_confirmation_email(owner_email, name)
    send_apply_notice_to_superadmin(name, result["business_id"])
    return redirect("/superadmin?message=Solicitud de alta registrada correctamente.")


def superadmin_negocios_detalle(business_id):
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
    from app import send_approved_invitation_email

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
        user["id"],
        user["email"],
        business_id,
        "business_approved",
        f"Negocio aprobado: {result['slug']} (slug: {result['slug']})",
        get_client_ip(),
    )
    # EMAIL 2: invitación al owner para definir contraseña
    send_approved_invitation_email(
        result["owner_email"],
        result["slug"],
        public_url(result["invitation_url"]),
        result.get("expires_at", ""),
        platform_service.invitation_lifetime_hours(),
    )
    return redirect(
        f"/superadmin/negocios/{business_id}?message=Negocio aprobado e invitación generada."
    )


def superadmin_negocios_desactivar(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ok = platform_service.set_business_active(business_id, False)
    if not ok:
        return redirect(
            f"/superadmin/negocios/{business_id}?error=No se pudo suspender el negocio."
        )
    platform_service.log_platform_action(
        user["id"],
        user["email"],
        business_id,
        "business_disabled",
        "Negocio suspendido",
        get_client_ip(),
    )
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio suspendido correctamente.")


def superadmin_negocios_activar(business_id):
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ok = platform_service.set_business_active(business_id, True)
    if not ok:
        return redirect(f"/superadmin/negocios/{business_id}?error=No se pudo activar el negocio.")
    platform_service.log_platform_action(
        user["id"],
        user["email"],
        business_id,
        "business_enabled",
        "Negocio reactivado",
        get_client_ip(),
    )
    return redirect(f"/superadmin/negocios/{business_id}?message=Negocio activado correctamente.")


def superadmin_negocios_reinviar(business_id):
    from app import send_approved_invitation_email

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
        return redirect(
            f"/superadmin/negocios/{business_id}?error=No se encontró el owner del negocio."
        )
    token, expires_at = platform_service.resend_invitation(
        business_id, owner["user_id"], owner["email"]
    )
    platform_service.log_platform_action(
        user["id"],
        user["email"],
        business_id,
        "invitation_resent",
        f"Nueva invitación enviada a {owner['email']}",
        get_client_ip(),
    )
    # EMAIL 2: nueva invitación al owner
    business = platform_service.get_business_by_id_platform(business_id)
    if business:
        send_approved_invitation_email(
            owner["email"],
            business["name"],
            public_url(f"/b/{business['slug']}/invitacion/{token}"),
            expires_at,
            platform_service.invitation_lifetime_hours(),
        )
    return redirect(f"/superadmin/negocios/{business_id}?message=Nueva invitación generada.")


def _visual_identity_form_context(business_id, *, message="", error=""):
    """Contexto común de la vista de identidad visual de un negocio."""
    business = platform_service.get_business_by_id_platform(business_id)
    if business is None:
        abort(404)
    config, locked = visual_identity_service.load_identity(business_id)
    return {
        "business": business,
        "config": config,
        "locked_options": locked,
        "presets": preset_names(),
        "presets_catalog": preset_catalog(),
        "palette": palette_summary(config),
        "contrast_pairs": contrast_pairs(config),
        "fonts": list(ALLOWED_FONTS),
        "color_keys": list(COLOR_KEYS),
        "media_keys": list(MEDIA_KEYS),
        "lockable_options": list(LOCKABLE_OPTIONS),
        "default_preset": DEFAULT_PRESET,
        "preview_url": f"/b/{business['slug']}",
        "message": message,
        "error": error,
    }


def superadmin_visual_identity():
    """Lista los negocios con su preset y color primario vigente."""
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    rows = []
    for business in platform_service.list_businesses_for_platform():
        config, locked = visual_identity_service.load_identity(business["id"])
        rows.append(
            {
                "id": business["id"],
                "name": business["name"],
                "slug": business["slug"],
                "preset": config.get("theme_preset", DEFAULT_PRESET),
                "primary_color": config.get("primary_color", ""),
                "locked_count": len(locked),
            }
        )
    return render_template(
        "superadmin.html",
        superadmin=user,
        section="visual_identity",
        identity_rows=rows,
        message=request.args.get("message", ""),
        error=request.args.get("error", ""),
    )


def superadmin_visual_identity_detail(business_id):
    """Formulario de edición de identidad visual de un negocio."""
    denied = _superadmin_gate()
    if denied:
        return denied
    user, _ = _platform_current_user()
    context = _visual_identity_form_context(
        business_id, message=request.args.get("message", ""), error=request.args.get("error", "")
    )
    return render_template(
        "superadmin.html", superadmin=user, section="visual_identity_detail", **context
    )


def superadmin_visual_identity_save(business_id):
    """Guarda la config visual completa (el Super Admin no está limitado por locks)."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    config = {key: request.form.get(key, "") for key in CONFIG_KEYS}
    result = visual_identity_service.save_identity(
        business_id, config, actor=user["email"], enforce_locks=False
    )
    if not result["success"]:
        detail = result.get("violations") or result.get("errors") or []
        reason = result["reason"]
        if detail:
            reason = f"{reason}: " + "; ".join(detail)
        return redirect(f"/superadmin/identidad/{business_id}?error={reason}")
    platform_service.log_platform_action(
        user["id"],
        user["email"],
        business_id,
        "visual_identity_updated",
        f"Identidad visual guardada (preset: {result['config'].get('theme_preset')})",
        get_client_ip(),
    )
    return redirect(f"/superadmin/identidad/{business_id}?message=Identidad visual guardada.")


def superadmin_visual_identity_preset(business_id):
    """Aplica un preset completo conservando la media cargada."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    preset = (request.form.get("theme_preset", "") or "").strip().lower()
    result = visual_identity_service.apply_preset(
        business_id, preset, actor=user["email"], enforce_locks=False
    )
    if not result["success"]:
        return redirect(
            f"/superadmin/identidad/{business_id}?error=No se pudo aplicar el preset ({preset})."
        )
    platform_service.log_platform_action(
        user["id"],
        user["email"],
        business_id,
        "visual_identity_preset",
        f"Preset visual aplicado: {preset}",
        get_client_ip(),
    )
    return redirect(f"/superadmin/identidad/{business_id}?message=Preset aplicado correctamente.")


def superadmin_visual_identity_locks(business_id):
    """Define las opciones que el tenant no podrá editar (locked_options)."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    locked = request.form.getlist("locked_options")
    result = visual_identity_service.save_locked_options(business_id, locked)
    if not result["success"]:
        return redirect(
            f"/superadmin/identidad/{business_id}?error=No se pudieron guardar los bloqueos."
        )
    platform_service.log_platform_action(
        user["id"],
        user["email"],
        business_id,
        "visual_identity_locks_updated",
        f"Bloqueos actualizados ({len(result['locked_options'])} opciones)",
        get_client_ip(),
    )
    return redirect(f"/superadmin/identidad/{business_id}?message=Bloqueos actualizados.")


# ============================================================
# SECCIÓN: INTELIGENCIA ARTIFICIAL (configuración global de proveedores)
# ============================================================

_PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "cerebras": "Cerebras",
}

_TEST_PROVIDER_CLASSES = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
    "cerebras": CerebrasProvider,
}


def _ia_redirect(*, message: str | None = None, error: str | None = None):
    """Construye un redirect IA codificando de forma segura mensajes y errores."""
    params = []
    if message:
        params.append(f"message={quote(message, safe='')}")
    if error:
        params.append(f"error={quote(error, safe='')}")
    query = f"?{'&'.join(params)}" if params else ""
    return redirect(f"/superadmin/ia{query}")


def _ia_context(*, message="", error=""):
    """Contexto común de la vista de proveedores de IA."""
    user, _ = _platform_current_user()
    providers = list_ai_providers()
    encryption_ready = encryption_is_configured()
    editing_provider = None
    edit_raw = (request.args.get("edit", "") or "").strip()
    if edit_raw.isdigit():
        editing_provider = get_ai_provider(int(edit_raw))
    return {
        "superadmin": user,
        "section": "ia",
        "providers": providers,
        "provider_labels": _PROVIDER_LABELS,
        "encryption_ready": encryption_ready,
        "editing_provider": editing_provider,
        "message": message,
        "error": error,
    }


def superadmin_ia():
    """Lista los proveedores de IA configurados (sección INTELIGENCIA ARTIFICIAL)."""
    denied = _superadmin_gate()
    if denied:
        return denied
    return render_template(
        "superadmin.html",
        **_ia_context(message=request.args.get("message", ""), error=request.args.get("error", "")),
    )


def superadmin_ia_guardar():
    """Crea o actualiza un proveedor de IA (credencial cifrada)."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ip = get_client_ip()
    if not _check_ia_rate_limit(ip, "ia_save"):
        return _ia_redirect(error="Demasiadas solicitudes. Esperá unos minutos.")

    provider = (request.form.get("provider", "") or "").strip().lower()
    model = (request.form.get("model", "") or "").strip()
    priority_raw = (request.form.get("priority", "") or "").strip()
    active_raw = (request.form.get("active", "") or "").strip()
    api_key = request.form.get("api_key", "") or ""
    provider_id_raw = (request.form.get("provider_id", "") or "").strip()

    if provider not in _PROVIDER_LABELS:
        return _ia_redirect(error="Proveedor no válido.")
    if not model:
        return _ia_redirect(error="El modelo es obligatorio.")
    try:
        existing = list_ai_providers()
        priority = (
            int(priority_raw)
            if priority_raw
            else (max((p.get("priority", 0) for p in existing), default=0) + 1)
        )
    except (TypeError, ValueError):
        return _ia_redirect(error="La prioridad debe ser un número entero.")

    provider_id = None
    if provider_id_raw:
        try:
            provider_id = int(provider_id_raw)
        except (TypeError, ValueError):
            provider_id = None

    try:
        if provider_id is not None:
            ok = update_ai_provider(
                provider_id,
                model=model or None,
                priority=priority or None,
                active=(active_raw == "1"),
                api_key=api_key.strip() or None,
                platform_user_id=user["id"],
                actor_email=user["email"],
                ip_address=ip,
            )
            if not ok:
                return _ia_redirect(error="Proveedor no encontrado.")
        else:
            create_ai_provider(
                provider=provider,
                api_key=api_key.strip(),
                model=model,
                priority=priority,
                active=(active_raw == "1"),
                platform_user_id=user["id"],
                actor_email=user["email"],
                ip_address=ip,
            )
        return _ia_redirect(message="Proveedor guardado correctamente.")
    except AIConfigError as exc:
        return _ia_redirect(error=str(exc))


def superadmin_ia_activar_desactivar(provider_id, active):
    """Activa o desactiva un proveedor de IA."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ip = get_client_ip()
    if not _check_ia_rate_limit(ip, "ia_toggle"):
        return _ia_redirect(error="Demasiadas solicitudes. Esperá unos minutos.")

    ok = set_ai_provider_active(
        provider_id, active, platform_user_id=user["id"], actor_email=user["email"], ip_address=ip
    )
    if not ok:
        return _ia_redirect(error="Proveedor no encontrado.")
    estado = "activado" if active else "desactivado"
    return _ia_redirect(message=f"Proveedor {estado} correctamente.")


def superadmin_ia_prioridad(provider_id):
    """Cambia la prioridad de un proveedor de IA."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ip = get_client_ip()
    if not _check_ia_rate_limit(ip, "ia_priority"):
        return _ia_redirect(error="Demasiadas solicitudes. Esperá unos minutos.")

    priority_raw = (request.form.get("priority", "") or "").strip()
    try:
        priority = int(priority_raw)
    except (TypeError, ValueError):
        return _ia_redirect(error="La prioridad debe ser un número entero.")

    try:
        ok = set_ai_provider_priority(
            provider_id,
            priority,
            platform_user_id=user["id"],
            actor_email=user["email"],
            ip_address=ip,
        )
    except AIConfigError as exc:
        return _ia_redirect(error=str(exc))
    if not ok:
        return _ia_redirect(error="Proveedor no encontrado.")
    return _ia_redirect(message="Prioridad actualizada correctamente.")


def superadmin_ia_eliminar(provider_id):
    """Elimina un proveedor de IA."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ip = get_client_ip()
    if not _check_ia_rate_limit(ip, "ia_delete"):
        return _ia_redirect(error="Demasiadas solicitudes. Esperá unos minutos.")

    ok = delete_ai_provider(
        provider_id, platform_user_id=user["id"], actor_email=user["email"], ip_address=ip
    )
    if not ok:
        return _ia_redirect(error="Proveedor no encontrado.")
    return _ia_redirect(message="Proveedor eliminado correctamente.")


def superadmin_ia_probar(provider_id):
    """Prueba la conexión con un proveedor de IA (sin ejecutar herramientas de reserva)."""
    denied = _superadmin_gate()
    if denied:
        return denied
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    user, _ = _platform_current_user()
    ip = get_client_ip()
    if not _check_ia_rate_limit(ip, "ia_test"):
        return _ia_redirect(error="Demasiadas solicitudes. Esperá unos minutos.")

    provider = get_ai_provider(provider_id)
    if provider is None:
        return _ia_redirect(error="Proveedor no encontrado.")

    provider_name = provider["provider"]
    label = _PROVIDER_LABELS.get(provider_name, provider_name)
    provider_cls = _TEST_PROVIDER_CLASSES.get(provider_name)
    if provider_cls is None:
        return _ia_redirect(error=f"Proveedor no soportado: {label}.")

    if not provider["has_credentials"]:
        return _ia_redirect(error="El proveedor no tiene credenciales configuradas.")

    try:
        api_key = get_decrypted_api_key(provider_id)
        if not api_key:
            return _ia_redirect(error="No se pudo descifrar la credencial del proveedor.")

        instance = provider_cls()
        contents = [{"role": "user", "parts": [{"text": "Responde únicamente con la palabra OK."}]}]
        response, _attempt, error_info = instance.generate(
            contents, None, api_key=api_key, model=provider.get("model") or None, timeout_s=5.0
        )
        if response is None:
            category, error_type = error_info or ("unknown", "UnknownError")
            return _ia_redirect(error=f"{label} no respondió: {category} ({error_type})")

        platform_service.log_platform_action(
            user["id"],
            user["email"],
            None,
            "ai_provider_test",
            f"provider={provider_name}, resultado=exitoso",
            ip,
        )
        return _ia_redirect(message=f"Prueba de conexión exitosa: {label} respondió correctamente.")
    except AIEncryptionError:
        return _ia_redirect(
            error="No se pudo descifrar la credencial (AI_ENCRYPTION_KEY ausente o inválida)."
        )
    except Exception:  # pragma: no cover - errores de red/proveedor externo
        logging.getLogger("turnobot.superadmin.ia").exception(
            "Prueba de conexión falló para %s", provider_name
        )
        return _ia_redirect(error="Prueba de conexión falló. Revisá los registros del servidor.")
