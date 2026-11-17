"""Gestión de archivos media (logos, portadas, favicons) e imágenes externas.

- Directorio persistente fuera del repositorio (TURNOBOT_MEDIA_DIR).
- Proxy de imágenes externas heredadas con protección SSRF, para mantener
  la CSP en `img-src 'self' data:` sin habilitar dominios HTTPS a lo brudo.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

# Extensiones permitidas → content-type esperado.
_ALLOWED_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "ico": "image/x-icon",
    "gif": "image/gif",
}

# Magic bytes mínimos por formato (validación sin Pillow).
_MAGIC_SIGNATURES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),  # + 'WEBP' en bytes 8..12 (verificado abajo)
    "ico": (b"\x00\x00\x01\x00",),
}

# Límites por tipo de archivo (bytes). El límite global de la app NO se amplía.
UPLOAD_LIMITS = {
    "logo": 512 * 1024,  # 512 KB
    "favicon": 64 * 1024,  # 64 KB
    "cover": 2 * 1024 * 1024,  # 2 MB
    "header_bg": 2 * 1024 * 1024,  # 2 MB
}

# Proxy de imágenes externas.
_EXTERNAL_PROXY_MAX_BYTES = 512 * 1024
_EXTERNAL_PROXY_TIMEOUT = 8
_EXTERNAL_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-\.]*[A-Za-z0-9])?$")


def get_media_dir() -> Path:
    """Directorio persistente de media, fuera del repositorio Git.

    TURNOBOT_MEDIA_DIR tiene prioridad; si no, se usa ~/.turnobot/media.
    Se crea bajo demanda. En producción se recomienda /var/lib/turnobot/media.
    """
    configured = (os.getenv("TURNOBOT_MEDIA_DIR") or "").strip()
    if configured:
        base = Path(configured).expanduser()
    else:
        base = Path.home() / ".turnobot" / "media"
    base.mkdir(parents=True, exist_ok=True)
    return base


def business_media_dir(business_id: int) -> Path:
    """Subdirectorio de media de un negocio (aislamiento por tenant)."""
    safe_id = int(business_id)
    path = get_media_dir() / f"business_{safe_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def allowed_extensions() -> set[str]:
    return set(_ALLOWED_IMAGE_TYPES)


def validate_image_file(file_storage, field_name: str) -> tuple[str | None, str | None]:
    """Valida un FileStorage de upload.

    Devuelve (ext, None) si es válido o (None, mensaje_de_error).
    """
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return None, "No se recibió ningún archivo."

    if field_name not in UPLOAD_LIMITS:
        return None, "Tipo de archivo no permitido."

    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_IMAGE_TYPES:
        return None, f"Formato no permitido. Usá: {', '.join(sorted(allowed_extensions()))}."

    # Límite de tamaño específico por tipo (no se amplía el global de la app).
    limit = UPLOAD_LIMITS[field_name]
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.stream.seek(0)
    if size <= 0:
        return None, "El archivo está vacío."
    if size > limit:
        max_kb = limit // 1024
        return None, f"El archivo supera el máximo de {max_kb} KB."

    # Magic bytes.
    header = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    signatures = _MAGIC_SIGNATURES.get(ext, ())
    ok = any(header.startswith(sig) for sig in signatures)
    if ext == "webp":
        ok = header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if not ok:
        return None, "El contenido del archivo no coincide con la extensión."

    return ext, None


def save_business_image(business_id: int, file_storage, field_name: str) -> str | None:
    """Guarda una imagen válida en el directorio del negocio.

    Devuelve la ruta pública relativa (/media/business_<id>/<uuid>.<ext>)
    o None si la validación falla (el error se loguea, no se lanza).
    """
    ext, error = validate_image_file(file_storage, field_name)
    if error or ext is None:
        logger.warning(
            "Upload rechazado business_id=%s field=%s: %s", business_id, field_name, error
        )
        return None

    directory = business_media_dir(business_id)
    name = f"{field_name}_{uuid.uuid4().hex}.{ext}"
    destination = directory / name
    file_storage.save(str(destination))
    return f"/media/business_{int(business_id)}/{name}"


def is_private_host(hostname: str) -> bool:
    """True si el host resuelve a loopback/privado/link-local/RESERVADO (SSRF)."""
    if not hostname:
        return True
    # IPv4/IPv6 literal.
    try:
        addr = ipaddress.ip_address(hostname)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        )
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True

    if not infos:
        return True
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return True
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return True
    return False


def fetch_external_image(url: str) -> tuple[bytes | None, str | None]:
    """Descarga una imagen externa con protección SSRF.

    Devuelve (bytes, None) o (None, mensaje). Solo http/https, hosts públicos,
    tamaño máximo y content-type imagen.
    """
    if not isinstance(url, str) or not url.strip():
        return None, "URL vacía."
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None, "Solo se permiten URLs http/https."
    if parts.username or parts.password:
        return None, "URL con credenciales no permitida."
    hostname = parts.hostname or ""
    if not hostname or not _EXTERNAL_HOST_RE.match(hostname):
        return None, "Host inválido."
    if is_private_host(hostname):
        return None, "Host no permitido."

    try:
        response = requests.get(
            url,
            timeout=_EXTERNAL_PROXY_TIMEOUT,
            stream=True,
            headers={"User-Agent": "TurnoBot-Media/1.0"},
        )
    except requests.RequestException:
        return None, "No se pudo descargar la imagen."

    try:
        if response.status_code != 200:
            return None, "La imagen remota no está disponible."
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            return None, "La URL no devuelve una imagen."

        # Acotar tamaño leyendo a lo sumo el máximo + 1.
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(8192):
            total += len(chunk)
            if total > _EXTERNAL_PROXY_MAX_BYTES:
                return None, "La imagen remota supera el tamaño máximo."
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            return None, "La imagen remota está vacía."
        return data, None
    finally:
        response.close()


def external_image_cache_path(url: str) -> Path:
    """Ruta local estable (por hash) para cachear una URL externa."""
    import hashlib

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    cache_dir = get_media_dir() / "external"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest}.img"


def content_type_for_path(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    # Archivos cacheados del proxy usan .img → sniff por firma.
    return _ALLOWED_IMAGE_TYPES.get(ext, "application/octet-stream")


def sniff_image_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    return "application/octet-stream"


def rewrite_external_logo_url(logo_url: str | None) -> str | None:
    """Reescribe URLs externas heredadas al proxy de mismo origen.

    Las URLs locales (/media/...) se devuelven tal cual. Las http(s) externas
    pasan por /media/external?url= para cumplir la CSP sin abrir dominios.
    """
    if not logo_url:
        return logo_url
    logo_url = str(logo_url).strip()
    if logo_url.startswith("/"):
        return logo_url
    if logo_url.startswith(("http://", "https://")):
        from urllib.parse import quote

        return f"/media/external?url={quote(logo_url, safe='')}"
    return logo_url
