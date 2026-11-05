"""Hostname validation and domain ownership/TLS verification helpers."""

import hashlib
import ipaddress
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urlsplit

_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_HOSTS = frozenset({"localhost"})
DOMAIN_VERIFICATION_TTL = "24 horas"


class InvalidHostname(ValueError):
    """Raised when a value is not a public DNS hostname."""


class DomainVerificationError(RuntimeError):
    """A controlled DNS/TLS verification failure safe to show to an operator."""


def normalize_hostname(
    value: str, *, allow_local: bool = False, allow_reserved: bool = False, allow_ip: bool = False
) -> str:
    """Return canonical lowercase IDNA hostname, rejecting authorities and IPs."""
    if not isinstance(value, str):
        raise InvalidHostname("Ingresá un nombre de dominio válido.")
    hostname = value.strip()
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if not hostname or hostname.endswith(".") or any(char.isspace() for char in hostname):
        raise InvalidHostname("Ingresá un nombre de dominio válido.")
    if any(char in hostname for char in "/:@?#[]%*\\"):
        raise InvalidHostname("Ingresá solo un nombre de dominio, sin protocolo ni puerto.")

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise InvalidHostname("El nombre de dominio no tiene un formato IDNA válido.") from error

    if len(ascii_hostname) > 253:
        raise InvalidHostname("El nombre de dominio supera la longitud permitida.")
    if allow_local and ascii_hostname in _LOCAL_HOSTS:
        return ascii_hostname
    if "." not in ascii_hostname:
        raise InvalidHostname("Usá un dominio público completo, por ejemplo www.ejemplo.com.")
    try:
        ip_address = ipaddress.ip_address(ascii_hostname)
    except ValueError:
        pass
    else:
        if allow_ip:
            return ip_address.compressed
        raise InvalidHostname("No se permiten direcciones IP como dominio.")
    if not allow_reserved and ascii_hostname.endswith(
        (".localhost", ".local", ".internal", ".test")
    ):
        raise InvalidHostname("No se permiten dominios locales o reservados.")
    labels = ascii_hostname.split(".")
    if any(not _DNS_LABEL_RE.fullmatch(label) for label in labels):
        raise InvalidHostname("El dominio contiene una etiqueta DNS inválida.")
    if len(labels[-1]) < 2:
        raise InvalidHostname("El dominio debe tener un sufijo público válido.")
    return ascii_hostname


def normalize_request_host(authority: str, scheme: str, *, allow_dev_ports: bool = False) -> str:
    """Parse an HTTP authority and return its normalized hostname."""
    try:
        parsed = urlsplit(f"//{authority}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InvalidHostname("El encabezado Host no es válido.") from error
    if not hostname or parsed.username or parsed.password or parsed.path or parsed.query:
        raise InvalidHostname("El encabezado Host no es válido.")

    normalized = normalize_hostname(hostname, allow_local=True, allow_reserved=True, allow_ip=True)
    if port is not None:
        expected_port = 443 if scheme == "https" else 80
        local_dev_port = allow_dev_ports and normalized in _LOCAL_HOSTS and 1 <= port <= 65535
        if port != expected_port and not local_dev_port:
            raise InvalidHostname("El puerto del encabezado Host no está permitido.")
    return normalized


def new_verification_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("ascii")).hexdigest()


def verification_record_name(hostname: str) -> str:
    return f"_turnobot-verification.{hostname}."


def check_dns_txt_verification(hostname: str, token_hash: str) -> None:
    """Check the exact DNS TXT token with bounded DNS resolution time."""
    import dns.exception
    import dns.resolver
    from dns.rdtypes.ANY.TXT import TXT

    resolver = dns.resolver.Resolver()
    resolver.timeout = 2.0
    try:
        answers = resolver.resolve(
            verification_record_name(hostname), "TXT", lifetime=5.0, search=False
        )
    except dns.resolver.NXDOMAIN as error:
        raise DomainVerificationError("No se encontró el registro TXT solicitado.") from error
    except dns.resolver.NoAnswer as error:
        raise DomainVerificationError("El dominio todavía no publica el registro TXT.") from error
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as error:
        raise DomainVerificationError(
            "El DNS no respondió a tiempo. Podés volver a intentar más tarde."
        ) from error
    except dns.exception.DNSException as error:
        raise DomainVerificationError(
            "No se pudo consultar el DNS. Podés volver a intentar más tarde."
        ) from error

    for record in answers:
        if not isinstance(record, TXT):
            continue
        try:
            candidate = b"".join(record.strings).decode("utf-8")
        except UnicodeDecodeError:
            continue
        candidate_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        if secrets.compare_digest(candidate_hash, token_hash):
            return
    raise DomainVerificationError("El valor TXT no coincide con el token vigente.")


def check_public_tls(hostname: str) -> None:
    """Verify a publicly reachable TLS certificate without following redirects."""
    try:
        address_info = socket.getaddrinfo(
            hostname, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError as error:
        raise DomainVerificationError("No se pudo resolver el dominio para HTTPS.") from error
    addresses: set[str] = set()
    for entry in address_info:
        address = entry[4][0]
        if not isinstance(address, str):
            raise DomainVerificationError("El DNS devolvió una dirección no válida.")
        addresses.add(address)
    if not addresses:
        raise DomainVerificationError("El dominio no tiene direcciones de red para HTTPS.")

    parsed_addresses = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as error:
            raise DomainVerificationError("El DNS devolvió una dirección no válida.") from error
        if not ip.is_global:
            raise DomainVerificationError(
                "La verificación HTTPS solo permite direcciones públicas."
            )
        parsed_addresses.append(address)

    context = ssl.create_default_context()
    deadline = time.monotonic() + 5.0
    for address in sorted(parsed_addresses):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainVerificationError("La verificación HTTPS excedió el tiempo permitido.")
            with socket.create_connection((address, 443), timeout=remaining) as connection:
                with context.wrap_socket(connection, server_hostname=hostname):
                    pass
        except (OSError, ssl.SSLError) as error:
            raise DomainVerificationError(
                "No se pudo validar un certificado HTTPS vigente para todas las direcciones DNS."
            ) from error
