"""Sesión de plataforma (SUPERADMIN) (Bloque 5B, Paso 7).

Helpers extraídos de app.py. La identidad de plataforma
(`platform_users`/`platform_sessions`) es COMPLETAMENTE independiente de la
identidad de negocio. Se persisten en claves de sesión distintas
(platform_user_id / platform_session_token) para que ambas identidades
coexistan dentro de la misma cookie sin interferir.
"""

import datetime
import os

from flask import session

from application.session_crypto import _hash_session_token, _now_iso
from database.database import get_platform_user_by_id, is_platform_session_valid

_PLATFORM_SESSION_LIFETIME_SECONDS = int(
    os.getenv("PLATFORM_SESSION_LIFETIME_SECONDS", str(8 * 3600))
)


def _platform_session_expires_at():
    delta = datetime.timedelta(seconds=_PLATFORM_SESSION_LIFETIME_SECONDS)
    return (datetime.datetime.now(datetime.UTC) + delta).strftime("%Y-%m-%d %H:%M:%S")


def _clear_platform_session():
    """Invalidate ONLY the platform session, preserving the business one."""
    session.pop("platform_user_id", None)
    session.pop("platform_session_token", None)


def _platform_current_user():
    """Devuelve el SUPERADMIN autenticado (sin password_hash) o (None, None)."""
    platform_user_id = session.get("platform_user_id")
    token = session.get("platform_session_token")
    if not platform_user_id or not token:
        return None, None
    user = get_platform_user_by_id(platform_user_id)
    if user is None or not user["active"]:
        _clear_platform_session()
        return None, None
    if not is_platform_session_valid(platform_user_id, _hash_session_token(token), _now_iso()):
        _clear_platform_session()
        return None, None
    safe_user = {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "active": bool(user["active"]),
    }
    return safe_user, token


def _is_platform_authenticated():
    user, _ = _platform_current_user()
    return user is not None


def _platform_actor_email():
    user, _ = _platform_current_user()
    return user["email"] if user else ""
