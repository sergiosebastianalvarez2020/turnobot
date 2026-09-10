"""Capa de negocio de la PLATAFORMA (SUPERADMIN).

Separada de `services/memberships.py` (negocios) y de `users/business_users`:
la identidad de plataforma vive en `platform_users` y opera sobre la
plataforma completa con prefijos `platform_*` en la capa de datos.

NO es tenant-scoped a propósito: estas operaciones las ejecuta SOLO el
SUPERADMIN autenticado contra `platform_users`. Ningún owner/admin de un
negocio llega aquí (gate en app.py + verificación de membresía nula).
"""

import hashlib
import os
import secrets
import datetime
from werkzeug.security import check_password_hash, generate_password_hash

from database.database import (
    create_business_with_owner,
    create_invitation,
    create_platform_session,
    create_platform_user,
    get_invitation_by_token_hash,
    get_platform_user_by_email,
    get_user_by_id_scoped,
    list_members_scoped,
    consume_invitation_atomically,
    revoke_active_invitations,
    set_business_pending,
)

# Re-export: helpers de datos de plataforma usados desde app.py
from database.database import (  # noqa: F401
    list_audit_log,
    list_businesses_for_platform,
    log_platform_action,
    get_business_by_id_platform,
    list_invitations,
    set_business_active,
    list_platform_users,
)


def invitation_lifetime_hours():
    try:
        return max(1, int(os.getenv("INVITATION_LIFETIME_HOURS", "72")))
    except (TypeError, ValueError):
        return 72


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invitation_expires_iso():
    delta = datetime.timedelta(hours=invitation_lifetime_hours())
    return (
        datetime.datetime.now(datetime.timezone.utc) + delta
    ).strftime("%Y-%m-%d %H:%M:%S")


def authenticate_superadmin(email, password):
    """Autentica un SUPERADMIN contra platform_users.

    Devuelve (platform_user, None) o (None, mensaje_error).
    Nunca autentica usuarios de negocio ni membresías.
    """
    if not email or not password:
        return None, "Credenciales inválidas."
    user = get_platform_user_by_email(email.strip().lower())
    if user is None or not user["active"]:
        return None, "Credenciales inválidas."
    if not check_password_hash(user["password_hash"], password):
        return None, "Credenciales inválidas."
    return user, None


def create_superadmin(email, password, display_name=""):
    """Crea un SUPERADMIN (usado por CLI/bootstrap, no por HTTP).

    Devuelve {"success": bool, "reason": str|None, "platform_user_id": int|None}.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"success": False, "reason": "invalid_email", "platform_user_id": None}
    if not password or len(password) < 12:
        return {"success": False, "reason": "weak_password", "platform_user_id": None}
    password_hash = generate_password_hash(password)
    platform_user_id = create_platform_user(email, password_hash, display_name or "", active=True)
    if platform_user_id is None:
        return {"success": False, "reason": "email_exists", "platform_user_id": None}
    log_platform_action(platform_user_id, email, None, "superadmin_created",
                        f"Superadmin creado: {email}", ip_address="")
    return {"success": True, "reason": None, "platform_user_id": platform_user_id}


def establish_platform_session(platform_user_id, token, expires_at_iso):
    """Registra la sesión persistente del SUPERADMIN (hash SHA-256 del token)."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return create_platform_session(platform_user_id, token_hash, expires_at_iso)


# ============================================================
# PROVISIÓN DE NEGOCIO + INVITACIÓN DEL OWNER
# ============================================================

def provision_business(name, slug, owner_email):
    """Crea un negocio PENDIENTE DE APROBACIÓN con owner pendiente (active=0).

    El negocio nace active=0, pending=1 y NO se genera invitación en este paso:
    el SUPERADMIN aprueba la solicitud con `approve_business()` y ahí recién se
    crea la invitación (EMAIL 2) para el owner.

    Devuelve {"success", "reason", "business_id", "slug", "user_id", "pending"}.
    """
    try:
        result = create_business_with_owner(name, owner_email, password=None, slug=slug)
    except ValueError as error:
        return {"success": False, "reason": str(error)}
    return {
        "success": True,
        "reason": None,
        "business_id": result["business_id"],
        "slug": result["slug"],
        "user_id": result["user_id"],
        "pending": True,
    }


def approve_business(business_id):
    """APRUEBA una solicitud de alta: marca el negocio como aprobado y activo,
    y genera la invitación única del owner.

    Solo aplica a negocios en estado pending=1 (active=0). Devuelve
    {"success", "reason", "business_id", "slug", "invitation_token",
     "invitation_url", "expires_at", "owner_email"}.
    El `invitation_token` (plaintext) solo se devuelve una vez; en DB solo
    vive el hash. Si el negocio no está pendiente devuelve reason="not_pending"
    y NO reenvía/regenera nada.
    """
    business = get_business_by_id_platform(business_id)
    if business is None:
        return {"success": False, "reason": "business_not_found"}
    if not (business.get("pending", 0) == 1 and not business.get("active", 0)):
        return {"success": False, "reason": "not_pending"}

    connection_result = _approve_and_link(business_id)
    if connection_result is None:
        return {"success": False, "reason": "approval_failed"}

    owner_email, user_id, slug = connection_result
    token = secrets.token_urlsafe(32)
    expires_at = _invitation_expires_iso()
    create_invitation(
        business_id, user_id, owner_email, "owner", hash_token(token), expires_at,
    )
    return {
        "success": True,
        "reason": None,
        "business_id": business_id,
        "slug": slug,
        "owner_email": owner_email,
        "invitation_url": f"/b/{slug}/invitacion/{token}",
        "expires_at": expires_at,
    }


def _approve_and_link(business_id):
    """Marca pending=0 / active=1 y devuelve (owner_email, user_id, slug).

    El negocio queda ACTIVO con la aprobación, pero el owner permanece como
    invitación pendiente (active=0) hasta que acepte la invitación: así el
    botón de reinvitación sigue disponible y el usuario solo valida tras
    crear su contraseña. La invitación jamás puede usarse antes de aprobar:
    la página pública exige `resolve_business` con active=1.
    """
    set_business_pending(business_id, 0)
    set_business_active(business_id, 1)
    business = get_business_by_id_platform(business_id)
    owner = _find_owner(business_id)
    if business is None or owner is None:
        return None
    return owner["email"], owner["user_id"], business["slug"]


def _find_owner(business_id):
    """Devuelve el owner de un negocio (dict con user_id, email)."""
    for member in list_members_scoped(business_id):
        if member["role_name"] == "owner":
            return {"user_id": member["user_id"], "email": member["email"]}
    return None


def accept_invitation(business_id, token, password):
    """El owner establece su contraseña y la invitación queda invalidada.

    Devuelve {"success": bool, "reason": str|None, "user_id": int|None}.
    """
    if not password or len(password) < 12:
        return {"success": False, "reason": "weak_password", "user_id": None}
    user_id = consume_invitation_atomically(
        business_id, hash_token(token), generate_password_hash(password)
    )
    if user_id is None:
        return {"success": False, "reason": "invalid_token", "user_id": None}
    return {"success": True, "reason": None, "user_id": user_id}


def get_invitation_for_business(business_id, token):
    """Devuelve la invitación activa para un negocio (o None)."""
    return get_invitation_by_token_hash(business_id, hash_token(token))


def resend_invitation(business_id, user_id, email):
    """Genera una nueva invitación. Devuelve (token_plaintext, expires_at)."""
    token = secrets.token_urlsafe(32)
    expires_at = _invitation_expires_iso()
    revoke_active_invitations(business_id, user_id)
    create_invitation(
        business_id, user_id, (email or "").strip().lower(), "owner",
        hash_token(token), expires_at,
    )
    return token, expires_at
