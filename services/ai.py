import logging
import os
import re
import socket
import threading
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from database.database import (
    get_active_services_scoped,
    get_business_settings_scoped,
    get_resource_scoped,
    get_resources_scoped,
    get_weekly_schedule_scoped,
)
from services.ai_providers.base import ProviderError, classify_status_code
from services.ai_router import router as ai_router
from services.appointments import (
    cancel_appointment,
    create_appointment,
    get_available_times,
    get_customer_appointments,
    reschedule_appointment,
)
from services.conversations import (
    add_conversation_message_scoped,
    get_or_create_conversation_session_scoped,
    get_or_create_public_conversation_session_scoped,
    request_human_handoff_scoped,
    track_question_scoped,
)
from services.knowledge import search_knowledge_scoped
from services.notifications import dispatch_booking_emails, notifications_enabled

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

logger = logging.getLogger("el_corte")

# Mensaje unificado para el error contact_limit (usado por reservar y reprogramar).
_CONTACT_LIMIT_MESSAGE = (
    "Alcanzaste el máximo de reservas activas con estos datos de contacto. "
    "Cancelá una reserva existente o contactá al negocio para seguir reservando."
)


# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

# Caché de clientes Gemini aislado por credencial.
# La clave es un hash SHA-256 de la API Key (nunca se almacena la clave en texto plano).
_clients: dict[str, genai.Client] = {}
_clients_lock = threading.Lock()


def _client_cache_key(api_key: str) -> str:
    """Devuelve un hash SHA-256 de la API Key para usar como clave de caché.

    Nunca almacena la API Key en texto plano ni la registra en logs.
    """
    import hashlib

    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def get_gemini_client(api_key: str | None = None, timeout_ms: int | None = None):
    """Obtiene un cliente de Gemini aislado por credencial.

    Cada API Key distinta obtiene su propia instancia de cliente.
    Si se recibe `api_key` explícitamente, tiene prioridad sobre GEMINI_API_KEY.
    La creación y actualización están protegidas por un lock para evitar
    condiciones de carrera en entornos concurrentes.
    """
    key = api_key or API_KEY
    if not key:
        raise RuntimeError("GEMINI_API_KEY no está configurada")

    # La clave de caché incluye el timeout_ms para evitar reutilizar un cliente
    # con un timeout diferente cuando se solicita un tiempo menor.
    timeout_ms = timeout_ms or int(os.getenv("GEMINI_TIMEOUT_MS", "8000"))
    cache_key = _client_cache_key(key) + f":{timeout_ms}"

    with _clients_lock:
        client = _clients.get(cache_key)
        if client is None:
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))
            _clients[cache_key] = client
        return client


def invalidate_gemini_client(api_key: str | None = None):
    """Invalida el cliente caché de una API Key específica.

    Útil cuando se rotan credenciales para forzar la creación de un nuevo
    cliente con la clave actualizada. Si no se recibe api_key, invalida
    el cliente de la clave legacy (GEMINI_API_KEY).
    """
    key = api_key or API_KEY
    if not key:
        return
    timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", "8000"))
    cache_key = _client_cache_key(key) + f":{timeout_ms}"
    with _clients_lock:
        _clients.pop(cache_key, None)


MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash")

DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"

MAX_TOOL_ITERATIONS = 5
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_LENGTH = 2_000
MAX_HISTORY_TOTAL_CHARS = int(os.getenv("GEMINI_MAX_TOTAL_CHARS", "8000"))
MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "1024"))

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.1
RETRY_BACKOFF_MAX = 1.0

_MUTATING_TOOLS = frozenset({"reservar_turno", "cancelar_turno", "reprogramar_turno"})

# ask_ai() delega en AIProviderRouter (services/ai_router), que gestiona
# proveedores y fallback automático. El router instancia internamente
# GeminiProvider, GroqProvider y futuros proveedores.


def get_business_identity(business_id):
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    settings = get_business_settings_scoped(business_id)
    return settings or {
        "business_name": "Mi negocio",
        "business_type": "Negocio",
        "business_description": "",
        "timezone": DEFAULT_TIMEZONE,
    }


# ============================================================
# GEMINI ERROR CLASSIFICATION & RETRY
# ============================================================

# Status strings used by the Google API (gemini status field on APIError).
_TRANSIENT_STATUSES = {"DEADLINE_EXCEEDED", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL"}


_GEMINI_PERMISSION_TYPE_TEXTS = frozenset(
    {"PERMISSION_DENIED", "PERMISSION_DENIED_ERROR", "IAM_PERMISSION_DENIED", "ACCESS_DENIED"}
)


def _classify_gemini_network_error(error):
    """Clasifica errores de red/timeout de httpx y del sistema.

    Returns:
        ProviderError normalizado o None si el error no es de red.
    """
    try:
        import httpx

        if isinstance(error, httpx.TimeoutException):
            return ProviderError(
                "timeout", "HttpTimeout", retry_same_provider=True, fallback_next_provider=True
            )
        if isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
            return ProviderError(
                "server",
                error.__class__.__name__,
                retry_same_provider=True,
                fallback_next_provider=True,
            )
    except ImportError:
        pass

    if isinstance(error, (ConnectionError, OSError)):
        return ProviderError(
            "server",
            error.__class__.__name__,
            retry_same_provider=True,
            fallback_next_provider=True,
        )
    return None


def _gemini_rule_permission(status_text, type_text, code, status):
    if status_text == "PERMISSION_DENIED" or type_text in _GEMINI_PERMISSION_TYPE_TEXTS:
        return True, ProviderError(
            "permission",
            status_text or type_text or "PERMISSION_DENIED",
            retry_same_provider=False,
            fallback_next_provider=True,
        )
    return False, None


def _gemini_rule_auth(status_text, type_text, code, status):
    if status_text == "UNAUTHENTICATED" or code == 401:
        return True, ProviderError(
            "auth",
            status_text or type_text or "UNAUTHENTICATED",
            retry_same_provider=False,
            fallback_next_provider=True,
        )
    return False, None


def _gemini_rule_not_found(status_text, type_text, code, status):
    if status_text == "NOT_FOUND" or code == 404:
        return True, ProviderError(
            "invalid_argument",
            status_text or "NOT_FOUND",
            retry_same_provider=False,
            fallback_next_provider=True,
        )
    return False, None


def _gemini_rule_invalid_argument(status_text, type_text, code, status):
    if status_text == "INVALID_ARGUMENT" or code == 400:
        return True, ProviderError(
            "invalid_argument",
            status_text or "INVALID_ARGUMENT",
            retry_same_provider=False,
            fallback_next_provider=False,
        )
    return False, None


def _gemini_rule_deadline(status_text, type_text, code, status):
    if status_text == "DEADLINE_EXCEEDED":
        return True, ProviderError(
            "timeout", "DEADLINE_EXCEEDED", retry_same_provider=True, fallback_next_provider=True
        )
    return False, None


def _gemini_rule_quota(status_text, type_text, code, status):
    if status_text == "RESOURCE_EXHAUSTED" or code == 429:
        return True, ProviderError(
            "quota",
            status_text or "RESOURCE_EXHAUSTED",
            retry_same_provider=True,
            fallback_next_provider=True,
        )
    return False, None


def _gemini_rule_server(status_text, type_text, code, status):
    if status_text in {"UNAVAILABLE", "INTERNAL"} or (isinstance(code, int) and 500 <= code < 600):
        return True, ProviderError(
            "server",
            status_text or f"HTTP{code}",
            retry_same_provider=True,
            fallback_next_provider=True,
        )
    return False, None


_GEMINI_API_RULES = (
    _gemini_rule_permission,
    _gemini_rule_auth,
    _gemini_rule_not_found,
    _gemini_rule_invalid_argument,
    _gemini_rule_deadline,
    _gemini_rule_quota,
    _gemini_rule_server,
)


def _classify_gemini_api_error(error):
    """Clasifica un genai_errors.APIError con la tabla de reglas de status/code."""
    status = getattr(error, "status", None)
    code = getattr(error, "code", None)
    error_type = getattr(error, "type", None)
    status_text = str(status).strip().upper() if status is not None else ""
    type_text = str(error_type).strip().upper() if error_type is not None else ""

    for rule in _GEMINI_API_RULES:
        matched, result = rule(status_text, type_text, code, status)
        if matched:
            return result
    if code == 403:
        return classify_status_code(403)
    if status is not None:
        return ProviderError(
            "unknown", str(status), retry_same_provider=False, fallback_next_provider=False
        )
    display = str(code) if code is not None else "APIError"
    return ProviderError(
        "unknown", display, retry_same_provider=False, fallback_next_provider=False
    )


def _classify_gemini_provider_error(error):
    """Clasifica errores de Gemini en un ProviderError normalizado."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        return ProviderError(
            "timeout", "TimeoutError", retry_same_provider=True, fallback_next_provider=True
        )

    network_error = _classify_gemini_network_error(error)
    if network_error is not None:
        return network_error

    if isinstance(error, genai_errors.APIError):
        return _classify_gemini_api_error(error)

    if isinstance(error, ValueError):
        return ProviderError(
            "invalid_argument",
            "ValueError",
            retry_same_provider=False,
            fallback_next_provider=False,
        )

    return ProviderError(
        "unknown", error.__class__.__name__, retry_same_provider=False, fallback_next_provider=False
    )


def _classify_gemini_error(error):
    """Clasifica un error de Gemini manteniendo el contrato legacy de 3 valores."""
    normalized = _classify_gemini_provider_error(error)
    return normalized.category, normalized.retry_same_provider, normalized.code


def _is_transient_error(error):
    """True si el error es transitorio y merece retry."""
    _, is_transient, _ = _classify_gemini_error(error)
    return is_transient


def _gemini_error_message(category):
    """Devuelve un mensaje amigable según la categoría de error."""
    messages = {
        "timeout": (
            "Disculpá, la consulta tardó demasiado. Por favor, intentá nuevamente en unos momentos."
        ),
        "quota": (
            "Disculpá, el servicio de IA está temporalmente con limitaciones. "
            "Por favor, intentá nuevamente en unos minutos."
        ),
        "auth": (
            "Disculpá, en este momento estoy teniendo un problema para procesar tu consulta. "
            "Verificá las credenciales configuradas."
        ),
        "permission": (
            "Disculpá, el servicio de IA no tiene permisos para procesar "
            "esta solicitud. Verificá la configuración del proveedor."
        ),
        "invalid_argument": (
            "Disculpá, no pude procesar tu consulta. Por favor, intentá formularla de otra manera."
        ),
        "policy_denied": (
            "Disculpá, el proveedor de IA denegó esta solicitud por su política de acceso."
        ),
        "malformed_response": ("Disculpá, el proveedor de IA devolvió una respuesta inesperada."),
        "server": (
            "Disculpá, en este momento estoy teniendo un problema para procesar tu consulta."
        ),
    }
    return messages.get(
        category,
        ("Disculpá, en este momento estoy teniendo un problema para procesar tu consulta."),
    )


def _resolve_requested_timeout_ms(timeout_s, configured_timeout_ms: int) -> int:
    """Convierte timeout_s a ms; si no se pidió, usa el configurado."""
    return int(timeout_s * 1000) if timeout_s is not None else configured_timeout_ms


def _gemini_deadline_error(last_error: ProviderError | None) -> ProviderError:
    """Error de deadline global: conserva el último error si existe."""
    return last_error or ProviderError(
        "timeout", "GlobalTimeout", retry_same_provider=True, fallback_next_provider=True
    )


def _gemini_exhausted_error(last_error: ProviderError | None) -> ProviderError:
    """Error final cuando se agotan los reintentos de Gemini."""
    return last_error or ProviderError("unknown", "UnknownError")


def _extract_usage_tokens(response) -> dict:
    """Extrae los conteos de tokens de usage_metadata (si están disponibles)."""
    tokens = {"prompt_tokens": None, "candidate_tokens": None, "total_tokens": None}
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return tokens
    if isinstance(usage, dict):
        tokens["prompt_tokens"] = usage.get("prompt_token_count")
        tokens["candidate_tokens"] = usage.get("candidates_token_count")
        tokens["total_tokens"] = usage.get("total_token_count")
    else:
        tokens["prompt_tokens"] = getattr(usage, "prompt_token_count", None)
        tokens["candidate_tokens"] = getattr(usage, "candidates_token_count", None)
        tokens["total_tokens"] = getattr(usage, "total_token_count", None)
    return tokens


def _classify_and_log_error(
    error, request_id, business_id, latency_ms, attempt, logger_func
) -> ProviderError:
    """Clasifica un error y registra la información.

    Encapsula la normalización y logging de errores.
    """
    normalized = _classify_gemini_provider_error(error)
    logger_func(
        request_id=request_id,
        business_id=business_id,
        latency_ms=latency_ms,
        attempt=attempt,
        status="error",
        error_type=normalized.code,
    )
    return normalized


def _calculate_backoff_delay(
    attempt: int, base_backoff: float, max_backoff: float, deadline_remaining: float | None = None
) -> float:
    """Calcula el delay de backoff para la próxima iteración.

    Returns el delay en segundos. Si el deadline restante es menor que el delay,
    retorna el valor que cabría en el deadline.
    """
    delay = min(base_backoff * (2 ** (attempt - 1)), max_backoff)
    if deadline_remaining is not None and deadline_remaining <= delay:
        return deadline_remaining
    return delay


def _handle_gemini_call_error(
    error, attempt, http_attempts, request_id, business_id, start, deadline_s
):
    """Clasifica un error de Gemini, decide el retry y espera el backoff.

    Returns:
        Tupla (retorno_a_devolver_o_None, last_error, http_attempts).
        ``retorno`` None => reintentar (ya se esperó el backoff).
    """
    latency_ms = int((monotonic() - start) * 1000)
    normalized = _classify_and_log_error(
        error, request_id, business_id, latency_ms, attempt, _log_gemini_call
    )
    if not normalized.retry_same_provider or attempt >= MAX_RETRIES:
        return (None, http_attempts, normalized), normalized, http_attempts

    delay = _calculate_backoff_delay(
        attempt,
        RETRY_BACKOFF_BASE,
        RETRY_BACKOFF_MAX,
        (deadline_s - monotonic()) if deadline_s else None,
    )
    if deadline_s is not None:
        remaining = deadline_s - monotonic()
        if remaining <= delay:
            return (
                (
                    None,
                    http_attempts,
                    ProviderError(
                        "timeout",
                        "GlobalTimeout",
                        retry_same_provider=True,
                        fallback_next_provider=True,
                    ),
                ),
                normalized,
                http_attempts,
            )

    import time as _time

    _time.sleep(delay)
    return None, normalized, http_attempts


def _call_gemini_with_retry(
    contents,
    config,
    request_id=None,
    business_id=None,
    api_key=None,
    timeout_s=None,
    deadline_s=None,
    idempotency_key=None,
    model=None,
):
    """Llama a Gemini con reintentos acotados y devuelve solicitudes HTTP reales."""
    configured_timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", "8000"))
    requested_timeout_ms = _resolve_requested_timeout_ms(timeout_s, configured_timeout_ms)
    base_timeout_ms = min(requested_timeout_ms, configured_timeout_ms)
    resolved_model = model or MODEL
    http_attempts = 0
    last_error: ProviderError | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        if deadline_s is not None:
            remaining = deadline_s - monotonic()
            if remaining <= 0:
                return None, http_attempts, _gemini_deadline_error(last_error)
            timeout_ms = min(base_timeout_ms, int(remaining * 1000))
        else:
            timeout_ms = base_timeout_ms

        if timeout_ms <= 0:
            return (
                None,
                http_attempts,
                ProviderError(
                    "timeout",
                    "GlobalTimeout",
                    retry_same_provider=True,
                    fallback_next_provider=True,
                ),
            )

        start = monotonic()
        try:
            client = get_gemini_client(api_key=api_key, timeout_ms=timeout_ms)
            http_attempts += 1
            response = client.models.generate_content(
                model=resolved_model, contents=contents, config=config
            )
        except Exception as error:
            outcome, last_error, http_attempts = _handle_gemini_call_error(
                error, attempt, http_attempts, request_id, business_id, start, deadline_s
            )
            if outcome is not None:
                return outcome
            continue

        latency_ms = int((monotonic() - start) * 1000)
        _log_gemini_call(
            request_id=request_id,
            business_id=business_id,
            latency_ms=latency_ms,
            attempt=attempt,
            status="success",
            error_type=None,
            **_extract_usage_tokens(response),
        )
        return response, http_attempts, None

    return None, http_attempts, _gemini_exhausted_error(last_error)


def _log_gemini_call(
    request_id=None,
    business_id=None,
    latency_ms=0,
    attempt=0,
    status="unknown",
    error_type=None,
    prompt_tokens=None,
    candidate_tokens=None,
    total_tokens=None,
):
    """Registra una llamada a Gemini con contexto estructurado y métricas de tokens."""
    logger.info(
        "gemini_call request_id=%s business_id=%s latency_ms=%d attempt=%d status=%s error_type=%s prompt_tokens=%s candidate_tokens=%s total_tokens=%s",
        request_id or "-",
        business_id or "-",
        latency_ms,
        attempt,
        status,
        error_type or "-",
        prompt_tokens if prompt_tokens is not None else "-",
        candidate_tokens if candidate_tokens is not None else "-",
        total_tokens if total_tokens is not None else "-",
    )


_MUTATION_CONFIRMATION_PATTERNS = {
    "reservar_turno": re.compile(
        r"qued[óo] reservad[oa]|fue reservad[oa]|reservad[oa] correctamente|se reserv[óo]",
        re.IGNORECASE,
    ),
    "cancelar_turno": re.compile(
        r"qued[óo] cancelad[oa]|fue cancelad[oa]|cancelad[oa] correctamente|se cancel[óo]",
        re.IGNORECASE,
    ),
    "reprogramar_turno": re.compile(
        r"qued[óo] reprogramad[oa]|fue reprogramad[oa]|reprogramad[oa] correctamente|se reprogram[óo]",
        re.IGNORECASE,
    ),
}


def _detect_unsafe_mutation_confirmation(text):
    if not text:
        return None
    for tool_name, pattern in _MUTATION_CONFIRMATION_PATTERNS.items():
        if pattern.search(text):
            return tool_name
    return None


def _safe_text_response(response_text, confirmed_mutations):
    if response_text is None:
        return response_text
    asserted = _detect_unsafe_mutation_confirmation(response_text)
    if asserted is not None and asserted not in confirmed_mutations:
        logger.warning(
            "Respuesta de Gemini afirmó confirmación de %s sin que la herramienta "
            "lo confirmara; se retorna mensaje seguro.",
            asserted,
        )
        return (
            "Disculpá, no se pudo confirmar la operación en este momento. "
            "Por favor, verificá los datos e intentá nuevamente."
        )
    return response_text


# ============================================================
# PROMPT PRINCIPAL
# ============================================================

SYSTEM_PROMPT = """
Sos el recepcionista virtual del negocio indicado en la sección
"NOMBRE DEL NEGOCIO".

Tu objetivo es ayudar a los clientes a:

- consultar servicios
- consultar precios
- consultar horarios
- consultar disponibilidad
- consultar recursos reservables
- reservar turnos
- consultar sus turnos
- cancelar turnos
- reprogramar turnos


============================================================
REGLAS GENERALES
============================================================

1. Respondé siempre en español.

2. Sé amable, natural y breve.

3. No inventes información.

4. Los servicios, precios y duraciones se informan en la sección
   "SERVICIOS ACTUALES". Usá exclusivamente esos datos.

5. Si el cliente pregunta por disponibilidad,
   utilizá consultar_disponibilidad.

6. Nunca inventes horarios disponibles.

7. Si el cliente quiere reservar un turno necesitás:

   - nombre
   - servicio
   - fecha
   - horario

8. El teléfono es obligatorio para reservar, consultar, cancelar o reprogramar.

8b. Si el cliente quiere reservar, pedile también su email cuando sea posible
    (no es obligatorio). El email se usa para enviar la confirmación y el
    recordatorio del turno. Acriptalo al reservar_turno con el parámetro email.
    Validá que tenga un formato de email; si lo que dio no parece un email,
    pedile que lo confirme.

9. Si falta algún dato necesario para reservar,
   preguntá solamente por el dato que falta.

10. Antes de confirmar una reserva,
    utilizá reservar_turno.

11. Nunca digas que una reserva fue realizada si
    reservar_turno no confirmó que fue creada.

12. Si el horario solicitado está ocupado,
    informalo y ofrecé horarios disponibles.

13. Si el cliente dice "mañana", "pasado mañana",
    "hoy", "el viernes", etc., calculá la fecha utilizando
    la fecha actual proporcionada por el sistema.

14. Si el cliente ya proporcionó un dato durante
    la conversación, no vuelvas a pedirlo.

15. Si una pregunta no tiene relación con el negocio,
    explicá amablemente que solamente podés ayudar
    con servicios y turnos.


============================================================
RECURSOS RESERVABLES
============================================================

Algunos negocios tienen recursos reservables (canchas, consultorios, cabinas, etc.).
La lista de recursos activos se encuentra en la sección "RECURSOS ACTIVOS".

16b. Si el cliente pregunta por recursos disponibles o quiere reservar un recurso específico,
     utilizá consultar_disponibilidad con el parámetro resource_id opcional.

16c. Si el cliente quiere reservar un turno con un recurso específico,
     inclúyelo en reservar_turno con el parámetro resource_id.
     El resource_id debe coincidir con uno de los recursos listados en "RECURSOS ACTIVOS".

16d. Si el negocio no tiene recursos (la lista está vacía),
     no preguntes por recursos ni los incluyas en la reserva.


============================================================
CONSULTAR TURNOS DEL CLIENTE
============================================================

16. Si el cliente quiere consultar, cancelar o reprogramar
    un turno, primero necesitás identificar sus turnos.

17. Si no conocés el nombre o el teléfono del cliente, pedilos.

18. Para buscar turnos también necesitás el management_token
    de uno de los turnos del cliente: se entrega al reservar
    y llega en el enlace de gestión por email. Sin ese token
    la herramienta NO devuelve turnos del cliente.

19. Si el cliente no tiene a mano el token, pedile el enlace
    de gestión o el management_token ANTES de llamar a
    buscar_turnos_cliente.

20. Con nombre, teléfono y management_token utilizá
    buscar_turnos_cliente. Si el cliente tiene varios turnos,
    NO elijas uno al azar: mostrá los relevantes y preguntá
    cuál quiere cancelar o reprogramar.


============================================================
CANCELAR
============================================================

21. Nunca canceles un turno sin identificar primero
    exactamente cuál quiere cancelar el cliente.

22. Utilizá cancelar_turno solamente cuando tengas
    el ID exacto del turno que el cliente quiere cancelar.

22b. Para cancelar necesitás el management_token del turno,
    que se devuelve al momento de reservar. Si el cliente
    no lo proporciona, pedilo explícitamente antes de
    llamar a cancelar_turno.

23. Si la cancelación fue exitosa, confirmala claramente.

24. Si la cancelación falla, no digas que fue cancelado.


============================================================
REPROGRAMAR
============================================================

25. Para reprogramar necesitás:

    - ID del turno
    - nueva fecha
    - nuevo horario
    - management_token del turno (se devuelve al reservar)

26. Utilizá reprogramar_turno para realizar el cambio.

27. La herramienta verifica si el nuevo horario está libre.

28. Si el nuevo horario está ocupado, informalo y ofrecé
    otros horarios disponibles.

29. Nunca digas que una reprogramación fue realizada
    si la herramienta no confirmó el cambio.


============================================================
IMPORTANTE
============================================================

30. Nunca muestres al cliente:

    - nombres de herramientas
    - JSON
    - errores internos
    - código
    - detalles técnicos de la base de datos

31. Utilizá siempre la información devuelta por las
    herramientas.

32. No inventes IDs de turnos.

33. No canceles ni reprogrames un turno solamente porque
    el cliente menciona una fecha u horario. Primero
    identificá el turno correspondiente.
"""


def get_services_prompt(business_id):
    """Genera la lista actual de servicios desde la base de datos."""
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    services = get_active_services_scoped(business_id)
    if not services:
        return "No hay servicios habilitados en este momento."

    return "\n".join(
        f"- {service['name']}: ${service['price']:,.0f}".replace(",", ".") + " "
        f"({service['duration']} min)"
        for service in services
    )


def get_resources_prompt(business_id=None):
    """Genera la lista actual de recursos desde la base de datos."""
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    resources = get_resources_scoped(business_id, only_active=True)
    if not resources:
        return "No hay recursos reservables en este momento."

    return "\n".join(f"- {resource['name']} (ID: {resource['id']})" for resource in resources)


def get_business_hours_prompt(business_id):
    """Genera los horarios semanales actuales desde la base de datos."""
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    lines = []
    for day, name in enumerate(day_names):
        schedule = get_weekly_schedule_scoped(day, business_id)
        if not schedule or not schedule["is_open"]:
            lines.append(f"- {name}: cerrado")
            continue

        periods = []
        for start, end in (
            (schedule["morning_start"], schedule["morning_end"]),
            (schedule["afternoon_start"], schedule["afternoon_end"]),
        ):
            if start and end:
                periods.append(f"{start} a {end}")
        lines.append(f"- {name}: {' / '.join(periods)}")
    return "\n".join(lines)


# ============================================================
# HERRAMIENTA 1
# CONSULTAR DISPONIBILIDAD
# ============================================================

consultar_disponibilidad_declaration = types.FunctionDeclaration(
    name="consultar_disponibilidad",
    description=(
        "Consulta los horarios disponibles del negocio "
        "para una fecha determinada. "
        "Si se especifica resource_id, filtra por ese recurso."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "fecha": {"type": "string", "description": ("Fecha en formato YYYY-MM-DD.")},
            "servicio": {
                "type": "string",
                "description": ("Nombre opcional del servicio para filtrar por duración."),
            },
            "resource_id": {
                "type": "integer",
                "description": (
                    "ID opcional del recurso reservable. "
                    "Si se especifica, devuelve disponibilidad solo para ese recurso. "
                    "Debe coincidir con un recurso de 'RECURSOS ACTIVOS'."
                ),
            },
        },
        "required": ["fecha"],
        "additionalProperties": False,
    },
)


# ============================================================
# HERRAMIENTA 1b
# CONSULTAR RECURSOS
# ============================================================

consultar_recursos_declaration = types.FunctionDeclaration(
    name="consultar_recursos",
    description=(
        "Devuelve la lista de recursos reservables activos del negocio. "
        "Útil cuando el cliente pregunta qué recursos están disponibles."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
)


# ============================================================
# HERRAMIENTA 2
# RESERVAR TURNO
# ============================================================

reservar_turno_declaration = types.FunctionDeclaration(
    name="reservar_turno",
    description=(
        "Crea una reserva real en la base de datos. "
        "Usar solamente cuando el cliente haya proporcionado "
        "todos los datos necesarios y quiera realizar la reserva."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "nombre": {"type": "string", "description": "Nombre del cliente."},
            "telefono": {"type": "string", "description": "Teléfono del cliente."},
            "email": {
                "type": "string",
                "description": (
                    "Email del cliente, en formato válido. Preguntarlo "
                    "si no lo dio. Es opcional pero recomendado para "
                    "enviar la confirmación y recordatorio."
                ),
            },
            "servicio": {"type": "string", "description": "Nombre exacto de un servicio activo."},
            "fecha": {"type": "string", "description": ("Fecha en formato YYYY-MM-DD.")},
            "hora": {"type": "string", "description": ("Hora en formato HH:MM.")},
            "resource_id": {
                "type": "integer",
                "description": (
                    "ID opcional del recurso reservable. "
                    "Si se especifica, reserva ese recurso específico. "
                    "Debe coincidir con un recurso de 'RECURSOS ACTIVOS'."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "Clave opcional de idempotencia para evitar reservas duplicadas. "
                    "Si se proporciona la misma clave en reintentos, se devuelve "
                    "la reserva original en lugar de crear una nueva."
                ),
            },
        },
        "required": ["nombre", "telefono", "servicio", "fecha", "hora"],
        "additionalProperties": False,
    },
)


# ============================================================
# HERRAMIENTA 3
# BUSCAR TURNOS DEL CLIENTE
# ============================================================

buscar_turnos_declaration = types.FunctionDeclaration(
    name="buscar_turnos_cliente",
    description=(
        "Busca los turnos confirmados de un cliente. "
        "Requiere el management_token de uno de sus turnos "
        "como comprobante de propiedad (se entrega al reservar). "
        "Debe utilizarse antes de cancelar o reprogramar "
        "un turno cuando sea necesario identificarlo."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "nombre": {"type": "string", "description": ("Nombre del cliente.")},
            "telefono": {
                "type": "string",
                "description": "Teléfono con el que se registró el turno.",
            },
            "management_token": {
                "type": "string",
                "description": "management_token de uno de los turnos del cliente (se devuelve al reservar y llega en el enlace de gestión). Sin ello la herramienta no devuelve turnos.",
            },
        },
        "required": ["nombre", "telefono"],
        "additionalProperties": False,
    },
)


# ============================================================
# HERRAMIENTA 4
# CANCELAR TURNO
# ============================================================

cancelar_turno_declaration = types.FunctionDeclaration(
    name="cancelar_turno",
    description=(
        "Cancela un turno confirmado utilizando su ID. "
        "Usar solamente cuando el cliente haya identificado "
        "claramente cuál turno quiere cancelar."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "appointment_id": {
                "type": "integer",
                "description": ("ID exacto del turno que el cliente quiere cancelar."),
            },
            "telefono": {
                "type": "string",
                "description": "Teléfono con el que se registró el turno.",
            },
            "nombre": {
                "type": "string",
                "description": (
                    "Nombre con el que se registró el turno. "
                    "Obligatorio cuando no se dispone del management_token."
                ),
            },
        },
        "required": ["appointment_id", "telefono", "management_token"],
        "additionalProperties": False,
    },
)


# Gemini receives the H5 secret explicitly; the service enforces it for new appointments.
cancelar_turno_declaration.parameters_json_schema["properties"]["management_token"] = {  # type: ignore[index]
    "type": "string",
    "description": "Token secreto devuelto al crear el turno; obligatorio para cancelar turnos.",
}

# ============================================================
# HERRAMIENTA 5
# REPROGRAMAR TURNO
# ============================================================

reprogramar_turno_declaration = types.FunctionDeclaration(
    name="reprogramar_turno",
    description=(
        "Reprograma un turno confirmado a una nueva fecha "
        "y horario. La herramienta verifica que el nuevo "
        "horario esté disponible."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "appointment_id": {
                "type": "integer",
                "description": ("ID exacto del turno que se quiere reprogramar."),
            },
            "telefono": {
                "type": "string",
                "description": "Teléfono con el que se registró el turno.",
            },
            "nombre": {
                "type": "string",
                "description": (
                    "Nombre con el que se registró el turno. "
                    "Obligatorio cuando no se dispone del management_token."
                ),
            },
            "management_token": {
                "type": "string",
                "description": "Token secreto devuelto al crear el turno; obligatorio para reprogramar turnos.",
            },
            "nueva_fecha": {
                "type": "string",
                "description": ("Nueva fecha en formato YYYY-MM-DD."),
            },
            "nueva_hora": {"type": "string", "description": ("Nueva hora en formato HH:MM.")},
        },
        "required": ["appointment_id", "telefono", "nueva_fecha", "nueva_hora", "management_token"],
        "additionalProperties": False,
    },
)


# ============================================================
# HERRAMIENTA: SOLICITAR ATENCIÓN HUMANA
# ============================================================

solicitar_atencion_humana_declaration = types.FunctionDeclaration(
    name="solicitar_atencion_humana",
    description=(
        "Deriva la conversación a una persona del negocio cuando la IA "
        "no puede resolver la consulta del cliente. Úsalo cuando el cliente "
        "pida explícitamente hablar con una persona, o cuando la información "
        "necesaria no esté disponible en el conocimiento del negocio, servicios, "
        "horarios o recursos."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "motivo": {
                "type": "string",
                "description": "Breve motivo por el que se solicita atención humana.",
            }
        },
        "required": ["motivo"],
        "additionalProperties": False,
    },
)


def _execute_solicitar_atencion_humana(
    arguments, business_id, session_id=None, idempotency_key=None
):
    """Ejecuta la herramienta de solicitud de atención humana."""
    try:
        # La sesión se marca como needs_human en la capa de persistencia
        if session_id:
            request_human_handoff_scoped(session_id, business_id)
        return {
            "success": True,
            "message": "Tu solicitud ha sido registrada. Una persona del negocio te contactará pronto.",
        }
    except Exception:
        logger.exception("Error solicitando atención humana.")
        return {"success": False, "error": "No se pudo solicitar atención humana."}


# ============================================================
# TOOL DE GEMINI
# ============================================================

BARBERIA_TOOL = types.Tool(
    function_declarations=[
        consultar_disponibilidad_declaration,
        consultar_recursos_declaration,
        reservar_turno_declaration,
        buscar_turnos_declaration,
        cancelar_turno_declaration,
        reprogramar_turno_declaration,
        solicitar_atencion_humana_declaration,
    ]
)


# ============================================================
# EJECUCIÓN DE HERRAMIENTAS
# ============================================================


def _execute_consultar_recursos(arguments, business_id, session_id=None, idempotency_key=None):
    try:
        resources = get_resources_scoped(business_id, only_active=True)
        recursos = [{"id": r["id"], "nombre": r["name"]} for r in resources]

        return {"success": True, "recursos": recursos}

    except Exception:
        logger.exception("Error consultando recursos.")

        return {"success": False, "error": "No se pudo consultar los recursos."}


def _execute_consultar_disponibilidad(
    arguments, business_id, session_id=None, idempotency_key=None
):
    fecha = arguments["fecha"]
    servicio = arguments.get("servicio")
    resource_id = arguments.get("resource_id")

    try:
        horarios = get_available_times(fecha, business_id, servicio, resource_id)

        return {"success": True, "fecha": fecha, "horarios_disponibles": horarios}

    except Exception:
        logger.exception("Error consultando disponibilidad.")

        return {"success": False, "error": "No se pudo consultar la disponibilidad."}


def _reserva_error_response(resultado):
    reason = resultado.get("reason")
    if reason == "contact_limit":
        return {"success": False, "reason": "contact_limit", "message": _CONTACT_LIMIT_MESSAGE}
    return {
        "success": False,
        "reason": reason,
        "message": "No se pudo reservar el turno solicitado.",
    }


def _execute_reservar_turno(arguments, business_id, session_id=None, idempotency_key=None):
    try:
        active_services_a = get_active_services_scoped(business_id)

        if arguments["servicio"] not in {row["name"] for row in active_services_a}:
            return {"success": False, "message": "El servicio solicitado no está disponible."}

        # Validar resource_id si se provee
        resource_id = arguments.get("resource_id")
        if resource_id is not None:
            # Verificar que el recurso existe y está activo en este negocio
            resources = get_resources_scoped(business_id, only_active=True)
            resource_ids = {r["id"] for r in resources}
            if resource_id not in resource_ids:
                return {"success": False, "message": "El recurso especificado no está disponible."}

        email = (arguments.get("email") or "").strip()
        if notifications_enabled(business_id) and not email:
            return {
                "success": False,
                "reason": "email_required",
                "message": (
                    "Para reservar necesito tu email, así te envío "
                    "la confirmación del turno. ¿Cuál es tu email?"
                ),
            }

        resultado = create_appointment(
            customer_name=arguments["nombre"],
            phone=arguments["telefono"],
            service=arguments["servicio"],
            appointment_date=arguments["fecha"],
            appointment_time=arguments["hora"],
            business_id=business_id,
            email=email or None,
            resource_id=resource_id,
            idempotency_key=idempotency_key,
        )

        if not resultado.get("success"):
            return _reserva_error_response(resultado)

        try:
            dispatch_booking_emails(
                business_id,
                {
                    "id": resultado.get("appointment_id"),
                    "customer_name": resultado.get("customer_name"),
                    "customer_email": resultado.get("customer_email"),
                    "service": resultado.get("service"),
                    "appointment_date": resultado.get("appointment_date"),
                    "appointment_time": resultado.get("appointment_time"),
                    "appointment_end": resultado.get("appointment_end"),
                },
                management_token=None,
                slug=None,
                public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
            )
        except Exception:
            logger.exception("Error enviando confirmación de turno")

        response = {
            "success": True,
            "appointment_id": resultado["appointment_id"],
            "management_token": resultado.get("management_token"),
            "message": ("El turno fue reservado correctamente."),
        }
        if resultado.get("resource_id"):
            response["resource_id"] = resultado["resource_id"]
            resource = get_resource_scoped(resultado["resource_id"], business_id)
            if resource:
                response["resource_nombre"] = resource["name"]

        return response

    except Exception:
        logger.exception("Error creando reserva.")

        return {"success": False, "error": "No se pudo crear la reserva."}


def _execute_buscar_turnos_cliente(arguments, business_id, session_id=None, idempotency_key=None):
    try:
        nombre = arguments["nombre"]

        telefono = arguments["telefono"]

        management_token = (arguments.get("management_token") or "").strip()

        if not management_token:
            return {
                "success": False,
                "error": (
                    "Necesito el management_token de uno de tus turnos "
                    "para buscarlos. Revisá el enlace de gestión que "
                    "recibiste al reservar."
                ),
            }

        turnos = get_customer_appointments(nombre, telefono, business_id, management_token)

        if turnos is None:
            return {
                "success": False,
                "error": (
                    "No pudimos verificar tu identidad. "
                    "Revisá el enlace de gestión que recibiste al reservar."
                ),
            }

        return {"success": True, "turnos": turnos}

    except Exception:
        logger.exception("Error buscando turnos del cliente.")

        return {"success": False, "error": "No se pudieron consultar los turnos."}


def _execute_cancelar_turno(arguments, business_id, session_id=None, idempotency_key=None):
    try:
        appointment_id = arguments["appointment_id"]

        # El management_token es obligatorio para cancelar
        management_token = arguments.get("management_token")
        if not management_token:
            return {
                "success": False,
                "reason": "missing_management_token",
                "message": "El token de gestión es obligatorio para cancelar un turno.",
            }

        resultado = cancel_appointment(
            appointment_id,
            arguments["telefono"],
            business_id,
            management_token,
            customer_name=arguments.get("nombre"),
        )

        if resultado:
            return {
                "success": True,
                "appointment_id": appointment_id,
                "message": ("El turno fue cancelado correctamente."),
            }

        return {
            "success": False,
            "message": (
                "No pudimos encontrar ese turno con los datos indicados. "
                "Verificá tu nombre y teléfono e intentá nuevamente."
            ),
        }

    except Exception:
        logger.exception("Error cancelando turno.")

        return {"success": False, "error": "No se pudo cancelar el turno."}


def _execute_reprogramar_turno(arguments, business_id, session_id=None, idempotency_key=None):
    try:
        appointment_id = arguments["appointment_id"]

        nueva_fecha = arguments["nueva_fecha"]

        nueva_hora = arguments["nueva_hora"]

        # El management_token es obligatorio para reprogramar
        management_token = arguments.get("management_token")
        if not management_token:
            return {
                "success": False,
                "reason": "missing_management_token",
                "message": "El token de gestión es obligatorio para reprogramar un turno.",
            }

        resultado = reschedule_appointment(
            appointment_id=appointment_id,
            new_date=nueva_fecha,
            new_time=nueva_hora,
            phone=arguments["telefono"],
            business_id=business_id,
            management_token=arguments.get("management_token"),
            customer_name=arguments.get("nombre"),
        )

        if resultado["success"]:
            return {
                "success": True,
                "appointment_id": appointment_id,
                "nueva_fecha": nueva_fecha,
                "nueva_hora": nueva_hora,
                "message": ("El turno fue reprogramado correctamente."),
            }

        reason = resultado.get("reason")

        if reason == "occupied":
            return {
                "success": False,
                "reason": "occupied",
                "message": ("El nuevo horario ya está ocupado."),
            }

        if reason == "invalid_time":
            return {
                "success": False,
                "reason": "invalid_time",
                "message": (
                    "El horario solicitado no pertenece al horario de atención del negocio."
                ),
            }

        if reason == "not_found":
            return {
                "success": False,
                "reason": "not_found",
                "message": (
                    "No pudimos encontrar ese turno con los datos indicados. "
                    "Verificá tu nombre y teléfono e intentá nuevamente."
                ),
            }

        if reason == "contact_limit":
            return {"success": False, "reason": "contact_limit", "message": _CONTACT_LIMIT_MESSAGE}

        return {"success": False, "message": ("No se pudo reprogramar el turno.")}

    except Exception:
        logger.exception("Error reprogramando turno.")

        return {"success": False, "error": "No se pudo reprogramar el turno."}


_TOOL_HANDLERS = {
    "consultar_recursos": _execute_consultar_recursos,
    "consultar_disponibilidad": _execute_consultar_disponibilidad,
    "reservar_turno": _execute_reservar_turno,
    "buscar_turnos_cliente": _execute_buscar_turnos_cliente,
    "cancelar_turno": _execute_cancelar_turno,
    "reprogramar_turno": _execute_reprogramar_turno,
    "solicitar_atencion_humana": _execute_solicitar_atencion_humana,
}


def execute_tool(name, arguments, business_id=None, session_id=None, idempotency_key=None):
    if business_id is None:
        return {"success": False, "error": "Contexto de negocio inválido."}

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return None
    return handler(arguments, business_id, session_id, idempotency_key)


# ============================================================
# CONSTRUIR HISTORIAL
# ============================================================


def build_contents(conversation, message):

    contents = []

    for item in conversation:
        role = item.get("role")

        content = item.get("content", "")

        if role == "assistant":
            role = "model"

        if role not in ("user", "model"):
            logger.warning("Rol desconocido descartado: %s", role)

            continue

        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=str(content))]))

    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))

    return contents


def truncate_history_by_total_chars(contents, max_total_chars=MAX_HISTORY_TOTAL_CHARS):
    """Trunca contenidos viejos para mantener el total de caracteres en límite.

    Conserva los mensajes más recientes. Descarta completamente los más antiguos
    cuando el total supera el límite. No trunca contenido parcial para no
    romper el formato esperado por Gemini.
    """
    while contents:
        total = 0
        for content in contents:
            for part in content.parts or []:
                if part.text:
                    total += len(part.text)
        if total <= max_total_chars:
            break
        contents.pop(0)

    return contents


# ============================================================
# FORMATEAR CONFIRMACIÓN DE RESERVA
# ============================================================


_DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _format_fecha_humana(fecha):
    """Devuelve (día_semana, dd/mm/aaaa) en español para una fecha ISO."""

    fecha_obj = datetime.strptime(fecha, "%Y-%m-%d")

    dia_semana = _DIAS_SEMANA[fecha_obj.weekday()]

    fecha_formateada = fecha_obj.strftime("%d/%m/%Y")

    return dia_semana, fecha_formateada


def format_reservation_confirmation(
    nombre, servicio, fecha, hora, business_id, resource_nombre=None
):

    business_name = get_business_identity(business_id)["business_name"]

    try:
        dia_semana, fecha_formateada = _format_fecha_humana(fecha)

        resource_text = f"\n🏟️ **{resource_nombre}**" if resource_nombre else ""

        return (
            f"¡Listo, {nombre}! Tu turno para "
            f"**{servicio}** quedó reservado correctamente.\n\n"
            f"📅 **{dia_semana} {fecha_formateada}**\n"
            f"🕐 **{hora} hs**{resource_text}\n\n"
            f"¡Te esperamos en **{business_name}**!"
        )

    except Exception:
        logger.exception("Error formateando confirmación.")

        return (
            f"¡Listo, {nombre}! Tu turno para "
            f"**{servicio}** el día "
            f"**{fecha} a las {hora} hs** "
            f"fue reservado correctamente.\n\n"
            f"¡Te esperamos en **{business_name}**!"
        )


# ============================================================
# FORMATEAR ERROR DE RESERVA
# ============================================================


def format_reservation_error(arguments, result):

    reason = result.get("reason")
    messages_by_reason = {
        "occupied": "Ese horario ya está ocupado. Elegí otro horario disponible.",
        "past_date": "No podés reservar un turno para una fecha que ya pasó.",
        "past_time": "Ese horario ya pasó. Elegí otro horario disponible.",
        "closed_day": "Ese día el negocio está cerrado.",
        "invalid_date": "La fecha indicada no es válida.",
        "invalid_time": "Ese horario no está disponible para la fecha elegida.",
        "invalid_service": "El servicio indicado no está disponible.",
    }

    if reason in messages_by_reason:
        message = messages_by_reason[reason]
    else:
        message = result.get("message")

    if message:
        nombre = arguments.get("nombre", "")

        if nombre:
            return f"Disculpá, {nombre}. {message}"

        return message

    return "Disculpá, no pude completar la reserva. Por favor, intentá nuevamente."


# ============================================================
# FORMATEAR CANCELACIÓN
# ============================================================


def format_cancellation_confirmation():

    return "Listo. Tu turno fue cancelado correctamente. El horario quedó nuevamente disponible."


# ============================================================
# FORMATEAR REPROGRAMACIÓN
# ============================================================


def format_reschedule_confirmation(fecha, hora, business_id):

    business_name = get_business_identity(business_id)["business_name"]

    try:
        dia_semana, fecha_formateada = _format_fecha_humana(fecha)

        return (
            "Listo. Tu turno fue reprogramado correctamente.\n\n"
            f"📅 **{dia_semana} {fecha_formateada}**\n"
            f"🕐 **{hora} hs**\n\n"
            f"¡Te esperamos en **{business_name}**!"
        )

    except Exception:
        logger.exception("Error formateando reprogramación.")

        return f"Listo. Tu turno fue reprogramado para **{fecha} a las {hora} hs**."


# ============================================================
# FORMATEAR TURNOS
# ============================================================


def format_customer_appointments(turnos):

    if not turnos:
        return (
            "⚠️ No encontramos turnos con esos datos. Verificá que el "
            "nombre y el teléfono estén escritos correctamente e intentá nuevamente."
        )

    lines = ["Estos son tus turnos confirmados:"]

    for turno in turnos:
        try:
            dia, fecha = _format_fecha_humana(turno["appointment_date"])

        except Exception:
            dia = ""

            fecha = turno["appointment_date"]

        lines.append(
            f"- **{turno['service']}** — {dia} {fecha} a las **{turno['appointment_time']} hs**"
        )

    return "\n".join(lines)


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================


def ask_ai(
    message,
    conversation=None,
    business_id=None,
    customer_phone=None,
    customer_name=None,
    customer_email=None,
    public_token=None,
    idempotency_key=None,
):

    request_id = None
    try:
        from flask import g

        request_id = getattr(g, "request_id", None)
    except Exception:
        pass

    if business_id is None:
        return "Disculpá, no se pudo identificar el negocio solicitado.", None, None

    if conversation is None:
        conversation = []

    # ========================================================
    # PERSISTENCIA DE CONVERSACIÓN
    # ========================================================

    session = None
    session_id = None
    # Preserve the anonymous visitor token supplied by the public route. The
    # route derives it from Flask's signed session, scoped to the business.
    human_intent_detected = False
    if customer_phone:
        session = get_or_create_conversation_session_scoped(
            business_id, customer_phone, customer_name, customer_email
        )
        if session:
            session_id = session["id"]
            # Guardar mensaje del usuario
            add_conversation_message_scoped(session_id, business_id, "user", message)
            # Registrar pregunta para analytics
            track_question_scoped(business_id, message)
            # Detectar intención de hablar con humano
            if _detect_human_intent(message):
                human_intent_detected = True
                request_human_handoff_scoped(session_id, business_id)
    else:
        session = get_or_create_public_conversation_session_scoped(business_id, public_token)
        if session:
            session_id = session["id"]
            public_token = session.get("public_token")
            # Guardar mensaje del usuario
            add_conversation_message_scoped(session_id, business_id, "user", message)
            # Registrar pregunta para analytics
            track_question_scoped(business_id, message)
            # Detectar intención de hablar con humano
            if _detect_human_intent(message):
                human_intent_detected = True
                request_human_handoff_scoped(session_id, business_id)

    # ========================================================
    # FECHA ACTUAL
    # ========================================================

    settings = get_business_settings_scoped(business_id)
    business_name = settings["business_name"] if settings else "Mi negocio"
    business_type = settings["business_type"] if settings else "Negocio"
    business_description = settings["business_description"] if settings else ""
    timezone_name = settings["timezone"] if settings else DEFAULT_TIMEZONE
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = DEFAULT_TIMEZONE
        timezone = ZoneInfo(DEFAULT_TIMEZONE)

    now = datetime.now(timezone)

    current_date = now.strftime("%Y-%m-%d")

    current_day = now.strftime("%A")

    # ========================================================
    # INSTRUCCIONES
    # ========================================================

    services_text = get_services_prompt(business_id)
    business_hours_text = get_business_hours_prompt(business_id)

    # Buscar conocimiento relevante del negocio para la pregunta del usuario
    knowledge_entries = search_knowledge_scoped(business_id, message, limit=3)
    knowledge_text = ""
    if knowledge_entries:
        knowledge_lines = []
        for entry in knowledge_entries:
            knowledge_lines.append(
                f"[INFO NEGOCIO] Pregunta: {entry['question']}\nRespuesta: {entry['answer']}"
            )
        knowledge_text = "\n\n--- CONOCIMIENTO DEL NEGOCIO ---\n\n" + "\n\n".join(knowledge_lines)

    human_intent_note = ""
    if human_intent_detected:
        human_intent_note = "\n\n--- NOTA: El cliente ha solicitado hablar con una persona. La derivación ya fue registrada. Informa al cliente que una persona se pondrá en contacto con él. ---"

    instructions = f"""
{SYSTEM_PROMPT}

============================================================
NOMBRE DEL NEGOCIO
============================================================

{business_name}

TIPO DE NEGOCIO

{business_type}

DESCRIPCIÓN DEL NEGOCIO

{business_description}

============================================================
SERVICIOS ACTUALES
============================================================

{services_text}

============================================================
HORARIOS ACTUALES
============================================================

{business_hours_text}
{knowledge_text}
{human_intent_note}

============================================================
FECHA ACTUAL
============================================================

{current_date}

DÍA ACTUAL:

{current_day}

ZONA HORARIA:

{timezone_name}

Utilizá esta fecha para interpretar:
"hoy", "mañana", "pasado mañana",
días de la semana y fechas relativas.
"""

    # ========================================================
    # CONSTRUIR HISTORIAL
    # ========================================================

    contents = build_contents(conversation, message)
    contents = truncate_history_by_total_chars(contents)

    # ========================================================
    # CONFIGURACIÓN GEMINI
    # ========================================================

    config = types.GenerateContentConfig(
        system_instruction=instructions,
        tools=[BARBERIA_TOOL],
        temperature=0.2,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        automatic_function_calling=(types.AutomaticFunctionCallingConfig(disable=True)),
    )

    # ========================================================
    # PRIMERA LLAMADA
    # ========================================================

    response, attempt, error_info = ai_router.generate(
        contents,
        config,
        request_id=request_id,
        business_id=business_id,
        idempotency_key=idempotency_key,
    )

    if response is None:
        category, error_type = error_info or ("unknown", "UnknownError")
        logger.error(
            "Router falló tras %d intento(s): category=%s error_type=%s",
            attempt,
            category,
            error_type,
        )
        return ai_router.contingency_message(), session_id, public_token

    # ========================================================
    # CICLO DE HERRAMIENTAS
    # ========================================================

    confirmed_mutations = set()

    iteration = 0

    while True:
        iteration += 1

        logger.info(
            "tool_loop request_id=%s business_id=%s iteration=%d",
            request_id or "-",
            business_id or "-",
            iteration,
        )

        if iteration > MAX_TOOL_ITERATIONS:
            logger.error("Se alcanzó el límite de iteraciones.")

            return (
                "Disculpá, estoy teniendo dificultades "
                "para resolver tu consulta. "
                "Por favor, intentá nuevamente.",
                session_id,
                public_token,
            )

        function_calls = []

        # ----------------------------------------------------
        # DETECTAR FUNCTION CALLS
        # ----------------------------------------------------

        if response.candidates:
            candidate = response.candidates[0]

            if candidate.content:
                for part in candidate.content.parts:
                    if part.function_call:
                        function_calls.append(part.function_call)

        # ----------------------------------------------------
        # SIN HERRAMIENTAS
        # ----------------------------------------------------

        if not function_calls:
            try:
                response_text = response.text
                safe_text = _safe_text_response(response_text, confirmed_mutations)

                logger.info(
                    "ask_ai_complete request_id=%s business_id=%s tool_iterations=%d",
                    request_id or "-",
                    business_id or "-",
                    iteration,
                )

                # Guardar respuesta del asistente
                if session_id:
                    add_conversation_message_scoped(session_id, business_id, "assistant", safe_text)
                    # Detectar si la respuesta indica necesidad de humano
                    if _detect_needs_human(safe_text):
                        request_human_handoff_scoped(session_id, business_id)

                return safe_text, session_id, public_token

            except Exception:
                logger.exception("No se pudo obtener texto de Gemini.")

                return ("Disculpá, no pude generar una respuesta.", session_id, public_token)

        # ----------------------------------------------------
        # GUARDAR RESPUESTA DEL MODELO
        # ----------------------------------------------------

        if response.candidates:
            contents.append(response.candidates[0].content)

        # ====================================================
        # EJECUTAR HERRAMIENTAS
        # ====================================================

        function_response_parts = []

        for function_call in function_calls:
            tool_name = function_call.name

            arguments = dict(function_call.args or {})

            logger.info("Herramienta solicitada: %s", tool_name)

            # ------------------------------------------------
            # EJECUTAR
            # ------------------------------------------------

            try:
                result = execute_tool(
                    tool_name, arguments, business_id, session_id, idempotency_key=idempotency_key
                )

            except Exception:
                logger.exception("Error ejecutando herramienta.")

                result = {
                    "success": False,
                    "error": "No se pudo procesar la respuesta del servicio de IA.",
                }

            logger.info(
                "Resultado de %s: success=%s reason=%s",
                tool_name,
                result.get("success"),
                result.get("reason"),
            )

            if (
                tool_name == "buscar_turnos_cliente"
                and result.get("success") is True
                and not result.get("turnos")
            ):
                return format_customer_appointments([]), session_id, public_token

            # =================================================
            # RESERVA CONFIRMADA
            # =================================================

            if tool_name == "reservar_turno" and result.get("success") is True:
                confirmed_mutations.add("reservar_turno")

                logger.info("RESERVA CONFIRMADA. No se realiza una segunda llamada a Gemini.")

                return (
                    format_reservation_confirmation(
                        nombre=arguments["nombre"],
                        servicio=arguments["servicio"],
                        fecha=arguments["fecha"],
                        hora=arguments["hora"],
                        business_id=business_id,
                        resource_nombre=result.get("resource_nombre"),
                    ),
                    session_id,
                    public_token,
                )

            # =================================================
            # RESERVA FALLIDA
            # =================================================

            if tool_name == "reservar_turno" and result.get("success") is False:
                return format_reservation_error(arguments, result), session_id, public_token

            # =================================================
            # CANCELACIÓN CONFIRMADA
            # =================================================

            if tool_name == "cancelar_turno" and result.get("success") is True:
                confirmed_mutations.add("cancelar_turno")

                logger.info("CANCELACIÓN CONFIRMADA.")

                return format_cancellation_confirmation(), session_id, public_token

            if (
                tool_name == "cancelar_turno"
                and result.get("success") is False
                and result.get("reason") != "missing_management_token"
            ):
                logger.info("CANCELACIÓN FALLIDA.")

                msg = result.get("message")
                if msg:
                    return f"Disculpá, {msg}", session_id, public_token
                return (
                    "Disculpá, no se pudo cancelar el turno. "
                    "Verificá los datos e intentá nuevamente.",
                    session_id,
                    public_token,
                )

            # =================================================
            # REPROGRAMACIÓN CONFIRMADA
            # =================================================

            if tool_name == "reprogramar_turno" and result.get("success") is True:
                confirmed_mutations.add("reprogramar_turno")

                logger.info("REPROGRAMACIÓN CONFIRMADA.")

                return (
                    format_reschedule_confirmation(
                        fecha=result["nueva_fecha"],
                        hora=result["nueva_hora"],
                        business_id=business_id,
                    ),
                    session_id,
                    public_token,
                )

            if (
                tool_name == "reprogramar_turno"
                and result.get("success") is False
                and result.get("reason") != "missing_management_token"
            ):
                logger.info("REPROGRAMACIÓN FALLIDA.")

                msg = result.get("message")
                if msg:
                    return f"Disculpá, {msg}", session_id, public_token
                return (
                    "Disculpá, no se pudo reprogramar el turno. "
                    "Verificá los datos e intentá nuevamente.",
                    session_id,
                    public_token,
                )

            # =================================================
            # RESPUESTAS DIRECTAS DE BÚSQUEDA
            #
            # En este punto dejamos que Gemini utilice
            # los resultados para continuar la conversación.
            # =================================================

            if tool_name in _MUTATING_TOOLS and not isinstance(result.get("success"), bool):
                logger.error(
                    "Herramienta mutadora %s devolvió un resultado sin "
                    "'success' booleano; no se confirma la operación ni se "
                    "alimenta el resultado a Gemini.",
                    tool_name,
                )
                return (
                    "Disculpá, no se pudo confirmar la operación en este momento. "
                    "Por favor, verificá los datos e intentá nuevamente.",
                    session_id,
                    public_token,
                )

            function_response_parts.append(
                types.Part.from_function_response(name=tool_name, response={"result": result})
            )

        # ====================================================
        # DEVOLVER RESULTADOS A GEMINI
        # ====================================================

        if function_response_parts:
            contents.append(types.Content(role="user", parts=function_response_parts))

        # ====================================================
        # CONTINUAR CON GEMINI
        # ====================================================

        # Si se confirmó una mutación (reserva, cancelación, reprogramación)
        # en alguna iteración previa del ciclo de herramientas, no volver a
        # llamar al router. Retornar inmediatamente con la confirmación
        # determinística ya generada. Esto evita que un fallo del router
        # posterior a una operación exitosa haga que el usuario reciba una
        # contingencia en lugar de la confirmación de su operación.
        if confirmed_mutations:
            return (
                "Su operación se procesó correctamente. "
                "Si no recibe la confirmación completa, verifique su estado "
                "contactando al negocio.",
                session_id,
                public_token,
            )

        response, attempt, error_info = ai_router.generate(
            contents,
            config,
            request_id=request_id,
            business_id=business_id,
            idempotency_key=idempotency_key,
        )

        if response is None:
            category, error_type = error_info or ("unknown", "UnknownError")
            logger.error(
                "Router falló durante tool-calling loop tras %d intento(s): category=%s error_type=%s",
                attempt,
                category,
                error_type,
            )
            return ai_router.contingency_message(), session_id, public_token


def _detect_needs_human(text):
    """Detecta si la respuesta de la IA indica que se necesita intervención humana.

    Busca patrones que sugieren que la IA no pudo responder adecuadamente.
    """
    if not text:
        return False
    text_lower = text.lower()
    # Patrones que indican que la IA no sabe responder
    patterns = [
        "no tengo esa información",
        "no dispongo de esa información",
        "no tengo acceso a esa información",
        "no puedo responder",
        "no sé",
        "no tengo conocimiento",
        "esa información no está disponible",
        "consultar con el negocio",
        "hablar con alguien",
        "contactar al negocio",
        "derivar a una persona",
        "hablar con una persona",
        "atención humana",
        "personal del negocio",
    ]
    return any(pattern in text_lower for pattern in patterns)


def _detect_human_intent(text):
    """Detecta si el USUARIO está pidiendo hablar con una persona.

    Se usa para ofrecer proactivamente el handoff humano.
    """
    if not text:
        return False
    text_lower = text.lower()
    patterns = [
        "hablar con una persona",
        "hablar con alguien",
        "hablar con un humano",
        "hablar con una persona real",
        "quiero hablar con alguien",
        "quiero hablar con una persona",
        "necesito hablar con alguien",
        "necesito hablar con una persona",
        "hablar con el dueño",
        "hablar con el encargado",
        "hablar con el responsable",
        "no me entendiste",
        "no me entiendes",
        "no entendiste",
        "no me entendés",
        "no me entendés nada",
        "no me sirve",
        "esto no sirve",
        "quiero cancelar",
        "cancelar todo",
        "hablar con atencion al cliente",
        "atencion al cliente",
        "soporte humano",
    ]
    return any(pattern in text_lower for pattern in patterns)


# ============================================================
# PRUEBA DIRECTA
# ============================================================

if __name__ == "__main__":
    print()

    print("=" * 60)

    print("PRUEBA DEL RECEPCIONISTA IA")

    print("=" * 60)

    print()

    pregunta = input("Cliente: ")

    respuesta = ask_ai(pregunta)

    print()

    print("RECEPCIONISTA:")

    print(respuesta)

    print()

    print("=" * 60)
