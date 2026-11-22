"""
services/ai_providers/base.py — Interfaz común para proveedores de IA.

Define el contrato que debe cumplir cualquier proveedor de inteligencia
artificial integrado en TurnoBot. La interfaz es intencionalmente mínima:
solo cubre lo que ask_ai() necesita para orquestar llamadas sin conocer
al proveedor concreto.

Cada proveedor concreto (Gemini, Groq, OpenRouter, Cerebras) implementa
esta interfaz encapsulando su propio SDK, formato de API, clasificacion
de errores y politica de reintentos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderError(tuple):
    """Error normalizado de proveedor compatible con ``(category, code)``."""

    category: str
    code: str
    retry_same_provider: bool
    fallback_next_provider: bool

    def __new__(
        cls,
        category: str,
        code: str,
        *,
        retry_same_provider: bool = False,
        fallback_next_provider: bool = False,
    ) -> ProviderError:
        instance = super().__new__(cls, (category, code))
        instance.category = category
        instance.code = code
        instance.retry_same_provider = retry_same_provider
        instance.fallback_next_provider = fallback_next_provider
        return instance

    def as_tuple(self) -> tuple[str, str]:
        return (self.category, self.code)


_AUTH_CODES = frozenset(
    {
        "HTTP401",
        "401",
        "UNAUTHENTICATED",
        "UNAUTHORIZED",
        "INVALID_API_KEY",
        "API_KEY_INVALID",
        "API_KEY_EXPIRED",
    }
)
_PERMISSION_CODES = frozenset(
    {
        "PERMISSION_DENIED",
        "PERMISSION_DENIED_ERROR",
        "IAM_PERMISSION_DENIED",
        "API_KEY_PERMISSION_DENIED",
        "ACCESS_DENIED",
        "FORBIDDEN",
        "INSUFFICIENT_PERMISSIONS",
    }
)
_GENERIC_403_CODES = frozenset({"HTTP403", "403", "POLICY_DENIED", "PERMISSION_DENIED"})
_INVALID_ARGUMENT_CODES = frozenset(
    {"HTTP400", "400", "INVALID_ARGUMENT", "BAD_REQUEST", "INVALID_ARGUMENT_ERROR"}
)
_MODEL_NOT_FOUND_CODES = frozenset(
    {"HTTP404", "404", "NOT_FOUND", "MODEL_NOT_FOUND", "MODEL_NOT_FOUND_ERROR"}
)
_QUOTA_CODES = frozenset(
    {
        "HTTP429",
        "429",
        "RATELIMITEXCEEDED",
        "RATE_LIMIT_EXCEEDED",
        "RESOURCE_EXHAUSTED",
        "QUOTA_EXCEEDED",
        "TOO_MANY_REQUESTS",
        "RATE_LIMIT",
        "RATE_LIMIT_ERROR",
    }
)
_TIMEOUT_CODES = frozenset(
    {
        "TIMEOUT",
        "TIMEOUTERROR",
        "HTTPTIMEOUT",
        "GLOBALTIMEOUT",
        "DEADLINEEXCEEDED",
        "REQUESTTIMEOUT",
    }
)
_SERVER_CODES = frozenset(
    {
        "INTERNAL",
        "INTERNALERROR",
        "UNAVAILABLE",
        "SERVICEUNAVAILABLE",
        "BADGATEWAY",
        "BAD_GATEWAY",
        "GATEWAY_TIMEOUT",
        "SERVER_ERROR",
        "INTERNAL_SERVER_ERROR",
        "CONNECTIONERROR",
        "NETWORKERROR",
        "CONNECTERROR",
    }
)
_MALFORMED_CODES = frozenset(
    {
        "MALFORMEDRESPONSE",
        "MALFORMEDJSON",
        "INVALIDJSON",
        "INVALIDRESPONSE",
        "EMPTYRESPONSE",
        "EMPTYCHOICES",
        "MALFORMEDCHOICE",
        "MISSINGMESSAGE",
        "MALFORMEDCONTENT",
        "MALFORMEDTOOLCALLS",
        "MALFORMATUSAGE",
    }
)


def _normalize_code(code: Any) -> str:
    if code is None:
        return ""
    return str(code).strip().upper().replace("-", "_").replace(" ", "_")


def _display_code(code: Any, default: str) -> str:
    text = str(code).strip() if code is not None else ""
    return text or default


def _is_permission_category(code: str, category: str) -> bool:
    """Entrada al bloque de permisos: 403 genéricos o categoría relacionada."""
    return code in _GENERIC_403_CODES or category in {"permission", "policy_denied", "forbidden"}


def _is_auth_category(code: str, category: str) -> bool:
    """Verifica si el error pertenece a la categoría de autenticación."""
    return code in _AUTH_CODES or category in {"auth", "unauthenticated", "unauthorized"}


def _is_model_not_found_category(code: str, category: str) -> bool:
    """Verifica si el código identifica un modelo inexistente (solo código)."""
    return code in _MODEL_NOT_FOUND_CODES


def _is_invalid_argument_category(code: str, category: str) -> bool:
    """Verifica si el error pertenece a la categoría de argumento inválido."""
    return code in _INVALID_ARGUMENT_CODES or category in {"invalid_argument", "bad_request"}


def _is_quota_category(code: str, category: str) -> bool:
    """Verifica si el error pertenece a la categoría de quota."""
    return code in _QUOTA_CODES or category in {"quota", "rate_limit", "rate_limited"}


def _is_timeout_category(code: str, category: str) -> bool:
    """Verifica si el error pertenece a la categoría de timeout."""
    return code in _TIMEOUT_CODES or category in {"timeout", "deadline_exceeded"}


def _is_server_category(code: str, category: str) -> bool:
    """Verifica si el error pertenece a la categoría de servidor."""
    return code in _SERVER_CODES or category in {"server", "network"}


def _is_server_5xx_code(code: str, category: str) -> bool:
    """Verifica si el código normalizado representa un HTTP 5xx."""
    numeric_code = code[4:] if code.startswith("HTTP") and code[4:].isdigit() else code
    return numeric_code.isdigit() and 500 <= int(numeric_code) < 600


def _is_malformed_category(code: str, category: str) -> bool:
    """Verifica si el error pertenece a la categoría de respuesta malformada."""
    return code in _MALFORMED_CODES or category in {"malformed_response", "malformed"}


# Reglas de clasificación en orden de precedencia. Cada entrada es
# (predicado, categoría resultante, retry_same_provider, fallback_next_provider).
# El orden importa: se aplica la primera regla que coincida.
_ERROR_RULES: tuple[tuple[Any, str, bool, bool], ...] = (
    (_is_auth_category, "auth", False, True),
    (_is_model_not_found_category, "invalid_argument", False, True),
    (_is_invalid_argument_category, "invalid_argument", False, False),
    (_is_quota_category, "quota", True, True),
    (_is_timeout_category, "timeout", True, True),
    (_is_server_category, "server", True, True),
    (_is_server_5xx_code, "server", True, True),
    (_is_malformed_category, "malformed_response", True, True),
)


def classify_provider_error(error: Any) -> ProviderError:
    """Convierte errores de proveedor o tuplas legacy en ``ProviderError``."""
    if isinstance(error, ProviderError):
        return error

    if isinstance(error, tuple) and len(error) >= 2:
        raw_category = error[0]
        raw_code = error[1]
    elif isinstance(error, dict):
        raw_category = error.get("category", "unknown")
        raw_code = error.get("code") or error.get("error_type")
    else:
        raw_category = "unknown"
        raw_code = getattr(error, "__class__", type(None)).__name__

    category = str(raw_category).strip().lower()
    code = _normalize_code(raw_code)
    display = _display_code(raw_code, "UnknownError")

    if _is_permission_category(code, category):
        if code in _PERMISSION_CODES:
            return ProviderError(
                "permission", display, retry_same_provider=False, fallback_next_provider=True
            )
        return ProviderError(
            "policy_denied", display, retry_same_provider=False, fallback_next_provider=False
        )

    for predicate, category_name, retry_same, fallback_next in _ERROR_RULES:
        if predicate(code, category):
            return ProviderError(
                category_name,
                display,
                retry_same_provider=retry_same,
                fallback_next_provider=fallback_next,
            )

    return ProviderError(
        "unknown", display, retry_same_provider=False, fallback_next_provider=False
    )


def _status_policy(status_code: int | None, display: str) -> ProviderError:
    """Devuelve la política mínima garantizada por el status HTTP."""
    if status_code == 401:
        return ProviderError(
            "auth", display, retry_same_provider=False, fallback_next_provider=True
        )
    if status_code == 403:
        return ProviderError(
            "policy_denied", display, retry_same_provider=False, fallback_next_provider=False
        )
    if status_code == 404:
        return ProviderError(
            "invalid_argument", display, retry_same_provider=False, fallback_next_provider=True
        )
    if status_code == 400:
        return ProviderError(
            "invalid_argument", display, retry_same_provider=False, fallback_next_provider=False
        )
    if status_code == 429:
        return ProviderError(
            "quota", display, retry_same_provider=True, fallback_next_provider=True
        )
    if status_code is not None and 500 <= status_code < 600:
        return ProviderError(
            "server", display, retry_same_provider=True, fallback_next_provider=True
        )
    return ProviderError("unknown", display)


def _refining_categories(status_code: int | None, display: str) -> frozenset[str] | None:
    """Categorías estructuradas admitidas para refinar cada status HTTP.

    Returns:
        Conjunto de categorías compatibles, vacío si el status descarta al
        código estructurado, o None si el status no define refinamiento
        (se usa entonces el código estructurado si no es desconocido).
    """
    if status_code == 429:
        return frozenset({"quota"})
    if status_code is not None and 500 <= status_code < 600:
        return frozenset({"server", "timeout"})
    if status_code == 401:
        return frozenset({"auth"})
    if status_code == 403:
        return frozenset({"permission", "policy_denied"})
    if status_code == 404:
        if _normalize_code(display) in _MODEL_NOT_FOUND_CODES:
            return frozenset({"invalid_argument"})
        return frozenset()
    if status_code == 400:
        if _normalize_code(display) in _INVALID_ARGUMENT_CODES:
            return frozenset({"invalid_argument"})
        return frozenset()
    return None


def classify_status_code(status_code: int | None, code: Any = None) -> ProviderError:
    """Clasifica un status HTTP y un código estructurado sin perder la política HTTP.

    Precedencia:
    1. Un código estructurado reconocido puede refinar la categoría cuando es compatible.
    2. Un código desconocido nunca anula la política del status HTTP.
    3. 429 conserva cuota; 5xx conserva servidor; 401 conserva autenticación;
       403 es `policy_denied` salvo permiso explícito; 400/404 son `invalid_argument`.
    4. Si el código reconocido contradice al status, gana el status HTTP.
    """
    display = _display_code(code, "")
    if not display and status_code is not None:
        display = "RateLimitExceeded" if status_code == 429 else f"HTTP{status_code}"
    if not display:
        display = "UnknownError"

    structured = classify_provider_error(("status", display))
    status_error = _status_policy(status_code, display)

    if status_error.category == "unknown":
        return structured
    allowed = _refining_categories(status_code, display)
    if allowed is None:
        return structured if structured.category != "unknown" else status_error
    if structured.category in allowed:
        return structured
    return status_error


class AIProvider(ABC):
    """Interfaz base para proveedores de IA."""

    @abstractmethod
    def provider_name(self) -> str:
        """Nombre identificador del proveedor (ej. 'gemini', 'groq')."""

    @abstractmethod
    def is_configured(self) -> bool:
        """True si el proveedor tiene credenciales y puede usarse."""

    @abstractmethod
    def generate(
        self,
        contents: Any,
        config: Any,
        business_id: int | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        timeout_s: float | None = None,
        deadline_s: float | None = None,
        idempotency_key: str | None = None,
        model: str | None = None,
    ) -> tuple[Any | None, int, ProviderError | None]:
        """
        Ejecuta una generacion de contenido a través del proveedor.

        Args:
            contents: Historial de mensajes en formato propio del proveedor.
            config: Configuracion de generacion en formato propio del proveedor.
            business_id: ID del negocio (para logging y trazabilidad).
            request_id: ID de la solicitud (para logging y trazabilidad).
            api_key: API Key opcional para sobrescribir la configurada por defecto.
            timeout_s: Tiempo máximo en segundos para la llamada. Si se provee,
                       el proveedor debe respetarlo como límite superior.
            deadline_s: Momento absoluto (time.monotonic()) en el que toda la
                       operación del router debe terminar. Si se provee, el
                       proveedor debe respetarlo como límite superior y no
                       iniciar reintentos ni esperas que lo excedan.

        Returns:
            Tupla (respuesta_o_None, solicitudes_http, informacion_de_error_o_None).
            ``solicitudes_http`` cuenta únicamente peticiones de red realizadas.
            ``informacion_de_error`` es un ``ProviderError`` o None si tuvo éxito.
        """

    @abstractmethod
    def classify_error(self, error: Exception) -> tuple[str, bool, str]:
        """
        Clasifica un error del proveedor.

        Returns:
            Tupla (categoria, es_transitorio, tipo_error).
            categoria: 'timeout', 'quota', 'auth', 'invalid_argument',
                       'server', 'unknown'.
            es_transitorio: True si el error puede reintentarse.
            tipo_error: string corto para logging.
        """

    @abstractmethod
    def is_transient_error(self, error: Exception) -> bool:
        """True si el error es transitorio y merece reintento."""

    @abstractmethod
    def error_message(self, category: str) -> str:
        """Mensaje amigable para el usuario segun la categoria de error."""
