"""Contexto tenant y de negocio (Bloque 5B, Paso 5).

resolve_business, get_current_business_id, load_current_business y los
context processors de negocio extraídos de app.py, manteniendo lógica idéntica.

Sin dependencia top-level de la fachada `app.py` (Bloque 7A): create_app() puede
importarse y ejecutarse standalone. Para preservar la compatibilidad histórica,
_load_seam() resuelve los símbolos que la fachada `app` re-exporta y que los
tests parchean (app.resolve_business, app.get_business_settings_scoped); si la
fachada no está importada (uso standalone), cae a las implementaciones locales.
"""

import sys
import uuid
from time import monotonic

from flask import abort, g, request

from database.database import (
    get_active_services_scoped,
    get_business_settings,
    get_business_settings_scoped,
    get_connection,
)

from application.logging_config import logger


def _load_seam(name):
    """Resuelve un símbolo del seam de compatibilidad con la fachada `app`.

    Devuelve el atributo de la fachada si el módulo `app` ya está importado
    (preservando monkeypatches históricos sobre `app.<name>`, incluido el
    fallback defensivo de sys.modules para el import circular durante la carga
    de app.py). Si no lo está, devuelve la implementación local del módulo.

    No importa `app`: en un proceso standalone, `app` nunca está en
    sys.modules y el acceso no provoca su carga.
    """
    facade = sys.modules.get("app")
    if facade is None:
        return globals()[name]
    return getattr(facade, name)


def resolve_business(slug=None):
    """Resuelve un negocio existente sin aceptar un identificador del cliente."""
    connection = get_connection()
    try:
        if slug is None:
            row = connection.execute(
                "SELECT id, name, slug FROM businesses WHERE id = 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id, name, slug FROM businesses WHERE slug = ? AND active = 1",
                (slug,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_current_business_id():
    """Devuelve el negocio asociado al request actual, si existe."""
    business = getattr(g, "current_business", None)
    return business["id"] if business else None


def get_active_services():
    """
    Obtiene los servicios activos del negocio actual.
    Si business_id no está disponible, retorna vacío (fallback seguro).
    """
    business_id = get_current_business_id()
    if not business_id:
        return {}

    try:
        rows = get_active_services_scoped(business_id)
        if not rows:
            return {}

        services = {}
        for row in rows:
            services[row["name"]] = {
                "price": row["price"],
                "duration": row["duration"],
            }
        return services
    except Exception:
        logger.exception("Error obteniendo servicios activos")
        return {}


def load_current_business():
    """Carga el contexto request-scoped en función del slug de la URL o del fallback por defecto."""
    view_args = request.view_args or {}
    slug = view_args.get("slug")

    # request_id
    request_id = request.headers.get("X-Request-ID", "").strip()
    if request_id:
        request_id = request_id[:64]
    else:
        request_id = uuid.uuid4().hex
    g.request_id = request_id
    g.endpoint = request.endpoint or "-"
    g.method = request.method
    g.path = request.path
    g.start_time = monotonic()

    if request.path.startswith("/b/"):
        if slug is None:
            g.current_business = None
            g.business_id = None
            return abort(404)
        g.current_business = _load_seam("resolve_business")(slug)
        if g.current_business is None:
            if hasattr(g, "current_business"):
                delattr(g, "current_business")
            g.business_id = None
            return abort(404)
        g.business_id = g.current_business.get("id") if isinstance(g.current_business, dict) else (g.current_business["id"] if g.current_business else None)
        return None

    g.current_business = _load_seam("resolve_business")()
    if g.current_business:
        g.business_id = g.current_business.get("id") if isinstance(g.current_business, dict) else (g.current_business["id"] if g.current_business else None)
    else:
        g.business_id = None
    return None


def _settings_value(settings, key, default=""):
    """Acceso seguro a una clave de settings (dict o sqlite3.Row).

    sqlite3.Row no soporta .get(); usa [] con try/except KeyError.
    """
    if not settings:
        return default
    try:
        return settings[key] or default
    except (KeyError, IndexError, TypeError):
        return default


def inject_admin_prefix():
    business = getattr(g, "current_business", None)
    if business is not None and business.get("id") != 1 and business.get("slug"):
        admin_prefix = f"/b/{business['slug']}"
    else:
        admin_prefix = ""
    return {"admin_prefix": admin_prefix}


def inject_business_settings():
    business_id = get_current_business_id()
    settings = (
        _load_seam("get_business_settings_scoped")(business_id)
        if business_id is not None
        else _load_seam("get_business_settings")()
    )
    return {
        "business_settings": settings,
        "business_name": _settings_value(settings, "business_name", "Mi negocio"),
        "business_type": _settings_value(settings, "business_type", "Negocio"),
        "business_initials": _settings_value(settings, "business_initials", ""),
        "business_description": _settings_value(settings, "business_description", ""),
        "timezone": _settings_value(settings, "timezone", "UTC"),
        "logo_url": _settings_value(settings, "logo_url", ""),
        "primary_color": _settings_value(settings, "primary_color", ""),
        "secondary_color": _settings_value(settings, "secondary_color", ""),
    }