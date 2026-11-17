"""Rutas de media: archivos subidos por negocio e proxy de imágenes externas.

- /media/business_<id>/<file>  → sirve uploads (aislado por tenant).
- /media/external?url=...      → proxy de imágenes externas heredadas con
  protección SSRF; mantiene CSP en img-src 'self' data:.
"""

from __future__ import annotations

import logging
import re

from flask import abort, send_file

from application.media import (
    external_image_cache_path,
    fetch_external_image,
    get_media_dir,
    rewrite_external_logo_url,  # noqa: F401  (re-export para tests/rutas)
    sniff_image_content_type,
)

logger = logging.getLogger(__name__)

_BUSINESS_MEDIA_RE = re.compile(r"^business_(\d+)$")
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9]+$")


def serve_business_media(filename):
    """Sirve un archivo de media de un negocio. path: business_<id>/<file>."""
    parts = (filename or "").split("/", 1)
    if len(parts) != 2:
        abort(404)
    directory_name, file_name = parts

    match = _BUSINESS_MEDIA_RE.match(directory_name)
    if not match or not _SAFE_FILE_RE.match(file_name):
        abort(404)

    base = get_media_dir().resolve()
    target = (base / directory_name / file_name).resolve()
    # Defensa path traversal: el destino debe quedar dentro de media/.
    if not str(target).startswith(str(base) + "/") and target.parent != base:
        # Windows: comparar con sep del SO vía Path
        if base not in target.parents:
            abort(404)
    if not target.is_file():
        abort(404)

    return send_file(target, conditional=True, max_age=3600)


def serve_external_image():
    """Proxy SSRF-safe de una imagen externa heredada (?url=...)."""
    from flask import request

    url = (request.args.get("url") or "").strip()
    if not url:
        abort(400)

    cache_path = external_image_cache_path(url)
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        return send_file(cache_path, conditional=True, max_age=86400)

    data, error = fetch_external_image(url)
    if error or data is None:
        logger.info("Proxy de imagen externa rechazado: %s", error)
        abort(404)

    cache_path.write_bytes(data)
    return send_file(
        cache_path, mimetype=sniff_image_content_type(data), conditional=True, max_age=86400
    )
