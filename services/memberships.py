"""Capa de autorización de memberships (separada de HTTP).

Concentra la política de gestión de usuarios/memberships de un negocio:

- Solo el rol `owner` (del negocio indicado) gestiona memberships.
- El owner puede asignar admin/staff/customer, NUNCA owner.
- El owner no puede revocarse a sí mismo ni revocar/alzar al último owner.
- admin/staff/customer no gestionan memberships.
- Toda operación queda scoped por `business_id` (tenant ya resuelto por la
  capa superior a partir del slug de la URL). Nunca se acepta un negocio
  aportado por el cliente como autoridad para seleccionar otro tenant.

Reutiliza los helpers scoped de database.database y devuelve un dict
`{"success": bool, "reason": str|None, ...}` para que los futuros endpoints
HTTP traduzcan la respuesta sin reproducir la política.
"""

from database.database import (
    change_membership_role_scoped,
    count_owners_scoped,
    create_membership_scoped,
    get_membership_scoped,
    get_role_id_scoped,
    list_members_scoped,
    revoke_membership_scoped,
)

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
ROLE_CUSTOMER = "customer"

# Roles que el owner puede asignar. Excluye explícitamente `owner` para
# impedir la creación/escalada de owners mediante esta capa.
ASSIGNABLE_BY_OWNER = {ROLE_ADMIN, ROLE_STAFF, ROLE_CUSTOMER}


def _actor_role(actor_user_id, business_id):
    """Devuelve el nombre del rol del actor en el negocio indicado, o None."""
    membership = get_membership_scoped(actor_user_id, business_id)
    return membership["role_name"] if membership else None


def _denied(reason):
    return {"success": False, "reason": reason}


def _role_name_to_id(role_name):
    return get_role_id_scoped(role_name)


def can_manage_memberships(actor_user_id, business_id):
    """True si el actor es owner del negocio indicado."""
    return _actor_role(actor_user_id, business_id) == ROLE_OWNER


def list_members(actor_user_id, business_id):
    """Lista los miembros del negocio indicado. Solo owner."""
    if _actor_role(actor_user_id, business_id) != ROLE_OWNER:
        return _denied("no tienes permisos para gestionar memberships")
    return {"success": True, "reason": None, "members": list_members_scoped(business_id)}


def invite_member(actor_user_id, business_id, target_user_id, role_name):
    """Agrega un usuario ya existente como miembro del negocio con un rol.

    `target_user_id` debe estar resuelto por la capa superior. Solo owner, y
    solo puede asignar admin/staff/customer.
    """
    if _actor_role(actor_user_id, business_id) != ROLE_OWNER:
        return _denied("no tienes permisos para gestionar memberships")
    if role_name not in ASSIGNABLE_BY_OWNER:
        return _denied("rol destino no permitido")
    role_id = _role_name_to_id(role_name)
    if role_id is None:
        return _denied("rol destino no válido")
    if not create_membership_scoped(target_user_id, business_id, role_name):
        return _denied("no se pudo agregar la membresía")
    return {"success": True, "reason": None}


def change_role(actor_user_id, business_id, target_user_id, new_role_name):
    """Cambia el rol de una membresía del negocio indicado. Solo owner.

    No permite asignar `owner` ni degradar al último owner del negocio.
    """
    if _actor_role(actor_user_id, business_id) != ROLE_OWNER:
        return _denied("no tienes permisos para gestionar memberships")
    if new_role_name not in ASSIGNABLE_BY_OWNER:
        return _denied("rol destino no permitido")

    target = get_membership_scoped(target_user_id, business_id)
    if not target:
        return _denied("el miembro no existe en este negocio")

    new_role_id = _role_name_to_id(new_role_name)
    if new_role_id is None:
        return _denied("rol destino no válido")

    if target["role_name"] == ROLE_OWNER and new_role_name != ROLE_OWNER:
        if count_owners_scoped(business_id) <= 1:
            return _denied("no se puede degradar al último owner del negocio")

    if not change_membership_role_scoped(target_user_id, business_id, new_role_id):
        return _denied("no se pudo cambiar el rol")
    return {"success": True, "reason": None}


def revoke_membership(actor_user_id, business_id, target_user_id):
    """Revoca la membresía de un miembro del negocio indicado. Solo owner.

    No permite revocarse a sí mismo ni revocar al último owner.
    """
    if _actor_role(actor_user_id, business_id) != ROLE_OWNER:
        return _denied("no tienes permisos para gestionar memberships")
    if target_user_id == actor_user_id:
        return _denied("no puedes revocar tu propia membresía")

    target = get_membership_scoped(target_user_id, business_id)
    if not target:
        return _denied("el miembro no existe en este negocio")

    if target["role_name"] == ROLE_OWNER:
        if count_owners_scoped(business_id) <= 1:
            return _denied("no se puede revocar al último owner del negocio")

    if not revoke_membership_scoped(target_user_id, business_id):
        return _denied("no se pudo revocar la membresía")
    return {"success": True, "reason": None}
