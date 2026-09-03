"""Bootstrap/seed del usuario owner inicial (negocio 1).

MECANISMO DE MIGRACIÓN, NO de autenticación normal.

Convierte la credencial administrativa histórica (env ADMIN_PASSWORD_HASH)
en un usuario owner del negocio 1:

    ADMIN_PASSWORD_HASH  ->  users.password_hash  ->  business_users(role=owner)

Una vez que el owner existe, la autenticación normal utiliza
users/business_users/sessions y NO depende de ADMIN_PASSWORD_HASH.
"""

from database.database import (
    create_membership_scoped,
    create_user_scoped,
    get_user_by_email_scoped,
    set_user_password_scoped,
)

DEFAULT_ADMIN_EMAIL = "admin@turnobot.local"


def _business_has_owner(business_id):
    """Determina si existe al menos un owner para el negocio."""
    return bool(_owners_for_business(business_id))


def _owners_for_business(business_id):
    from database.database import get_connection

    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT u.id AS user_id
            FROM business_users bu
            JOIN roles r ON r.id = bu.role_id
            JOIN users u ON u.id = bu.user_id
            WHERE bu.business_id = ? AND r.name = 'owner'
            """,
            (business_id,),
        ).fetchall()
        return list(rows)
    finally:
        connection.close()


def provision_owner_from_bootstrap(business_id, email, password_hash):
    """
    Provisiona un owner para un negocio si aún no existe ninguno,
    asociando el usuario (creado si hace falta) con rol owner.
    Retorna el user_id o None.
    """
    if _business_has_owner(business_id):
        return None
    email = (email or DEFAULT_ADMIN_EMAIL).strip().lower()
    user = get_user_by_email_scoped(email)
    if user is None:
        user_id = create_user_scoped(email, password_hash, active=True)
        if user_id is None:
            return None
    else:
        user_id = user["id"]
    create_membership_scoped(user_id, business_id, "owner")
    return user_id


def migrate_owner_from_module_hash(business_id, email, password_hash):
    """
    MECANISMO DE MIGRACIÓN (negocio 1).
    Convierte la credencial administrativa histórica (module/env
    ADMIN_PASSWORD_HASH) en el owner del negocio 1 dentro del modelo
    users/business_users:
      - si no existe owner: lo provisiona con este hash;
      - si ya existe owner: sincroniza su hash al de ADMIN_PASSWORD_HASH
        para que el modelo de usuarios quede gobernado por él.
    Retorna el user_id del owner (o None si no aplica).
    """
    if business_id != 1:
        return None

    owner = _owners_for_business(business_id)
    if owner:
        user_id = owner[0]["user_id"]
        set_user_password_scoped(user_id, password_hash)
        return user_id

    return provision_owner_from_bootstrap(business_id, email, password_hash)