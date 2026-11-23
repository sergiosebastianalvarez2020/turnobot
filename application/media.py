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
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

logger = logging.getLogger(__name__)

# Número de puerto por defecto por esquema.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

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


def _ip_address_is_forbidden(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True si la dirección IP está prohibida por políticas SSRF.

    Política de allowlist: solo se permiten IPs globalmente alcanzables
    (addr.is_global == True). Se bloquea cualquier dirección que no sea
    global, incluyendo:
    - Loopback (127.0.0.0/8, ::1)
    - Privadas RFC 1918 (10/8, 172.16/12, 192.168/16)
    - Link-local (169.254/16, fe80::/10) — incluye metadata cloud (169.254.169.254)
    - CGNAT RFC 6598 (100.64.0.0/10) — no bloqueado por is_private en Python
    - Reservadas (192.0.2/24, 198.51.100/24, 203.0.113/24, 240/4)
    - Multicast (224/4, ff00::/8)
    - Unspecified (0.0.0.0, ::)
    - IPv6 Unique Local (fc00::/7)
    - IPv4-mapped IPv6 con IP no global (::ffff:10.x.x.x, ::ffff:100.64.x.x, etc.)

    Válido para IPv4, IPv6 y direcciones IPv4-mapped en IPv6 (::ffff:*).
    """
    return not addr.is_global


def is_private_host(hostname: str) -> bool:
    """True si el host resuelve a loopback/privado/link-local/RESERVADO (SSRF).

    La función resuelve el hostname y comprueba TODAS las IPs devueltas.
    Si cualquiera de ellas está prohibida, devuelve True (fail-secure).
    """
    if not hostname:
        return True
    # IPv4/IPv6 literal.
    try:
        addr = ipaddress.ip_address(hostname)
        return _ip_address_is_forbidden(addr)
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
        if _ip_address_is_forbidden(addr):
            return True
    return False


def _resolve_and_validate_host(
    hostname: str,
) -> tuple[list[tuple[socket.AddressFamily, str]], str | None]:
    """Resuelve el hostname y valida que ninguna IP sea privada/interna.

    Devuelve (lista de (family, ip_str), None) si todas las IPs son públicas,
    o ([], mensaje_de_error) si alguna IP es prohibida o hay error DNS.

    Esta función se usa para obtener las IPs validadas que luego se pasan
    directamente al adaptador HTTP, eliminando el gap TOCTOU entre la
    validación de host y la conexión TCP.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return [], "No se pudo resolver el host."

    if not infos:
        return [], "El host no resolvió a ninguna dirección."

    results: list[tuple[socket.AddressFamily, str]] = []
    for info in infos:
        family = info[0]
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return [], "Dirección IP inválida."
        if _ip_address_is_forbidden(addr):
            return [], "Host no permitido."
        results.append((family, raw))

    return results, None


# ---------------------------------------------------------------------------
# Adaptador HTTP con IP pinning para eliminar el gap TOCTOU de DNS rebinding.
#
# Estrategia:
#   1. _resolve_and_validate_host() resuelve el hostname y valida cada IP.
#   2. _SSRFSafeAdapter recibe la IP validada y la hostname original.
#   3. get_connection_with_tls_context() / get_connection() crean un pool cuyo
#      host (y por tanto _dns_host de la conexión TCP) es la IP numérica.
#   4. send() inyecta explícitamente el header "Host: <hostname_original>" en la
#      request para que el servidor reciba el virtualhost correcto.
#   5. Para HTTPS: server_hostname=hostname_original en el pool (SNI correcto) y
#      assert_hostname=hostname_original (verificación del certificado por nombre,
#      no por IP).
#
# El resultado: la conexión TCP va a la IP validada, sin segunda resolución DNS.
# TLS verifica el certificado con el nombre de dominio original.
# ---------------------------------------------------------------------------


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    """Pool HTTPS que usa server_hostname=hostname_original para SNI y verificación TLS,
    pero conecta la TCP a la IP pinneada (host del pool)."""

    def __init__(self, hostname: str, **kwargs):
        # hostname es el nombre de dominio original; se usa para SNI y assert_hostname.
        # 'host' en kwargs es la IP pinneada (destino TCP).
        super().__init__(**kwargs)
        self._original_hostname = hostname

    def _new_conn(self) -> HTTPSConnection:
        self.num_connections += 1
        conn = HTTPSConnection(
            host=self.host,  # IP numérica → conexión TCP directa a la IP pinneada.
            port=self.port,
            timeout=self.timeout.connect_timeout,
            cert_file=self.cert_file,
            key_file=self.key_file,
            key_password=self.key_password,
            cert_reqs=self.cert_reqs,
            ca_certs=self.ca_certs,
            ca_cert_dir=self.ca_cert_dir,
            assert_hostname=self._original_hostname,  # verificar cert por nombre de dominio.
            assert_fingerprint=self.assert_fingerprint,
            server_hostname=self._original_hostname,  # SNI correcto.
            ssl_version=self.ssl_version,
            ssl_minimum_version=self.ssl_minimum_version,
            ssl_maximum_version=self.ssl_maximum_version,
            **self.conn_kw,
        )
        return conn


class _SSRFSafeAdapter(HTTPAdapter):
    """HTTPAdapter que fuerza la conexión TCP a una IP pre-validada.

    Elimina el gap TOCTOU del DNS rebinding: la misma IP que fue validada
    por _resolve_and_validate_host() es la que se usa para la conexión TCP.

    Protecciones adicionales contra proxies:
    - get_connection_with_tls_context() ignora el dict de proxies; siempre
      devuelve el pool pinneado a la IP validada, sin pasar por ProxyManager.
    - request_url() devuelve siempre el path relativo, nunca la URL absoluta
      que requests generaría si detectara un proxy HTTP en el entorno.
    - La sesión que usa este adaptador debe tener trust_env=False y no pasar
      proxies, para que merge_environment_settings() no lea HTTP_PROXY/HTTPS_PROXY.

    TLS:
    - No se desactiva: SNI y verificación del certificado usan el hostname
      original, no la IP numérica.
    - El header "Host:" se inyecta explícitamente con el hostname original para
      que el servidor reciba el virtualhost correcto (ya que pool.host es la IP).
      Se incluye el puerto solo cuando no es el predeterminado del esquema.
    """

    def __init__(self, hostname: str, pinned_ip: str, **kwargs):
        super().__init__(**kwargs)
        self._hostname = hostname
        self._pinned_ip = pinned_ip

    def _build_pool(self, scheme: str, url_parts) -> HTTPConnectionPool:
        port = url_parts.port or _DEFAULT_PORTS.get(scheme, 80)
        timeout = urllib3.util.timeout.Timeout(
            connect=_EXTERNAL_PROXY_TIMEOUT, read=_EXTERNAL_PROXY_TIMEOUT
        )
        if scheme == "https":
            return _PinnedHTTPSConnectionPool(
                hostname=self._hostname, host=self._pinned_ip, port=port, timeout=timeout
            )
        return HTTPConnectionPool(host=self._pinned_ip, port=port, timeout=timeout)

    def _build_host_header(self, request) -> str:
        """Construye el valor del header Host: hostname + puerto si no es predeterminado."""
        parsed = urlsplit(request.url)
        default_port = _DEFAULT_PORTS.get(parsed.scheme)
        port = parsed.port
        if port is not None and default_port is not None and port == default_port:
            port = None
        host = self._hostname
        # Los nombres IPv6 requieren corchetes en el header Host.
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            host = f"[{host}]"
        if port is not None:
            host = f"{host}:{port}"
        return host

    def send(self, request, *args, **kwargs):
        # Inyectar Host: con el hostname original y puerto si corresponde.
        # urllib3 usará skip_host=1 al ver este header y no lo sobreescribirá
        # con la IP numérica.
        request.headers["Host"] = self._build_host_header(request)
        return super().send(request, *args, **kwargs)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        # Siempre devolver el pool pinneado; ignorar proxies explícitamente.
        # Esto evita que, si proxies contiene entradas del entorno, requests
        # cree un ProxyManager que resuelva el hostname por su cuenta.
        # `verify` y `cert` se conservan en la firma por compatibilidad con
        # la interfaz de HTTPAdapter de Requests; no se usan porque el pool
        # pinneado ya configura TLS con el hostname original.
        _ = (verify, cert)
        parsed = urlsplit(request.url)
        return self._build_pool(parsed.scheme, parsed)

    def get_connection(self, url, proxies=None):
        # Fallback para versiones antiguas de requests (deprecated en 2.32.2).
        # Igual que get_connection_with_tls_context: ignorar proxies.
        parsed = urlsplit(url)
        return self._build_pool(parsed.scheme, parsed)

    def request_url(self, request, proxies):
        # Devolver siempre el path relativo, independientemente de si hay
        # proxies configurados en el entorno. requests normalmente retornaría
        # la URL absoluta para proxies HTTP, pero aquí la conexión TCP ya va
        # directamente a la IP pinneada y no hay intermediario.
        parsed = urlsplit(request.url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path


def fetch_external_image(url: str) -> tuple[bytes | None, str | None]:
    """Descarga una imagen externa con protección SSRF completa.

    Protecciones aplicadas:
    - Esquema restringido a http/https.
    - Sin credenciales en URL.
    - Host validado contra caracteres permitidos.
    - Resolución DNS pre-validada: todas las IPs del host deben ser públicas.
    - IP pinning: la misma IP validada se usa para la conexión TCP (elimina
      el gap TOCTOU del DNS rebinding).
    - Redirecciones desactivadas: ninguna redirección se sigue automáticamente.
      Las redirecciones se rechazan para evitar SSRF via open redirect.
    - Content-Type verificado (debe ser image/*).
    - Tamaño máximo aplicado.
    - Cierre garantizado de la respuesta en todos los caminos.

    Devuelve (bytes, None) o (None, mensaje).
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

    # Resolver y validar el hostname ANTES de conectar.
    # La lista de IPs validadas se pasa al adaptador para que la conexión
    # TCP vaya directamente a la primera IP, sin segunda resolución DNS.
    resolved, resolve_error = _resolve_and_validate_host(hostname)
    if resolve_error or not resolved:
        return None, resolve_error or "Host no permitido."

    # Usar la primera IP resuelta para la conexión (misma que fue validada).
    pinned_ip = resolved[0][1]

    session = requests.Session()
    # trust_env=False: impide que requests lea HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
    # del entorno (via merge_environment_settings / resolve_proxies).
    # Cualquier proxy configurado en el entorno no debe interferir con la
    # conexión directa a la IP validada.
    session.trust_env = False
    adapter = _SSRFSafeAdapter(hostname=hostname, pinned_ip=pinned_ip)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        response = session.get(
            url,
            timeout=_EXTERNAL_PROXY_TIMEOUT,
            stream=True,
            allow_redirects=False,  # SSRF: nunca seguir redirecciones automáticamente.
            proxies={},  # Garantía adicional: dict vacío para que select_proxy() devuelva None.
            headers={"User-Agent": "TurnoBot-Media/1.0"},
        )
    except requests.RequestException:
        return None, "No se pudo descargar la imagen."

    try:
        # Redirecciones (3xx) se rechazan explícitamente para evitar SSRF via
        # open redirect hacia direcciones internas (127.0.0.1, 169.254.169.254, etc.).
        if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
            return None, "Redirección no permitida."
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
