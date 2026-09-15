import logging
import os
import re
import socket
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
)
from services.notifications import dispatch_booking_emails, notifications_enabled
from database.database import (
    get_active_services,
    get_active_services_scoped,
    get_business_settings,
    get_business_settings_scoped,
    get_weekly_schedule,
    get_weekly_schedule_scoped,
    get_resources_scoped,
    get_resource_scoped,
)
from services.knowledge import search_knowledge_scoped
from services.conversations import (
    get_or_create_conversation_session_scoped,
    get_or_create_public_conversation_session_scoped,
    add_conversation_message_scoped,
    track_question_scoped,
    request_human_handoff_scoped,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("el_corte")


# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
client = None


def get_gemini_client():
    global client
    if client is None:
        if not API_KEY:
            raise RuntimeError("GEMINI_API_KEY no está configurada")
        timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", "15000"))
        client = genai.Client(
            api_key=API_KEY,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
    return client


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
_TRANSIENT_STATUSES = {
    "DEADLINE_EXCEEDED",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "INTERNAL",
}


def _classify_gemini_error(error):
    """Clasifica un error de Gemini en una categoría para logging y retry.

    Devuelve (category, is_transient, error_type).
    - category: 'timeout', 'quota', 'auth', 'invalid_argument', 'server', 'unknown'
    - is_transient: True si puede reintentarse
    - error_type: string corto para logging
    """
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout", True, "TimeoutError"

    try:
        import httpx
        if isinstance(error, httpx.TimeoutException):
            return "timeout", True, "HttpTimeout"
        if isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
            return "server", True, error.__class__.__name__
    except ImportError:
        pass

    if isinstance(error, ConnectionError):
        return "server", True, "ConnectionError"

    if isinstance(error, OSError):
        return "server", True, "OSError"

    if isinstance(error, (genai_errors.APIError,)):
        status = getattr(error, "status", None)
        code = getattr(error, "code", None)
        if status == "UNAUTHENTICATED" or code == 401:
            return "auth", False, "UNAUTHENTICATED"
        if status == "PERMISSION_DENIED" or code == 403:
            return "auth", False, "PERMISSION_DENIED"
        if status == "INVALID_ARGUMENT" or code == 400:
            return "invalid_argument", False, "INVALID_ARGUMENT"
        if status == "NOT_FOUND" or code == 404:
            return "invalid_argument", False, "NOT_FOUND"
        if status == "DEADLINE_EXCEEDED":
            return "timeout", True, "DEADLINE_EXCEEDED"
        if status == "RESOURCE_EXHAUSTED" or code == 429:
            return "quota", True, "RESOURCE_EXHAUSTED"
        if status == "UNAVAILABLE" or code == 503:
            return "server", True, "UNAVAILABLE"
        if status == "INTERNAL" or (isinstance(code, int) and 500 <= code < 600):
            return "server", True, "INTERNAL"
        if status is not None:
            return "unknown", False, str(status)
        return "unknown", False, "APIError"

    if isinstance(error, ValueError):
        return "invalid_argument", False, "ValueError"

    return "unknown", False, error.__class__.__name__


def _is_transient_error(error):
    """True si el error es transitorio y merece retry."""
    _, is_transient, _ = _classify_gemini_error(error)
    return is_transient


def _gemini_error_message(category):
    """Devuelve un mensaje amigable según la categoría de error."""
    messages = {
        "timeout": (
            "Disculpá, la consulta tardó demasiado. "
            "Por favor, intentá nuevamente en unos momentos."
        ),
        "quota": (
            "Disculpá, el servicio de IA está temporalmente con limitaciones. "
            "Por favor, intentá nuevamente en unos minutos."
        ),
        "auth": (
            "Disculpá, en este momento estoy teniendo "
            "un problema para procesar tu consulta."
        ),
        "invalid_argument": (
            "Disculpá, no pude procesar tu consulta. "
            "Por favor, intentá formularla de otra manera."
        ),
        "server": (
            "Disculpá, en este momento estoy teniendo "
            "un problema para procesar tu consulta."
        ),
    }
    return messages.get(category, (
        "Disculpá, en este momento estoy teniendo "
        "un problema para procesar tu consulta."
    ))


def _call_gemini_with_retry(contents, config, request_id=None, business_id=None):
    """Llama a Gemini con retry acotado para errores transitorios.

    Registra latencia, intentos, tipo de error y uso de tokens.
    Devuelve (response, attempt_count, error_info).
    """
    last_error = None
    attempt = 0
    for attempt in range(1, MAX_RETRIES + 1):
        start = monotonic()
        try:
            response = get_gemini_client().models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )
            latency_ms = int((monotonic() - start) * 1000)

            prompt_tokens = None
            candidate_tokens = None
            total_tokens = None
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                if isinstance(usage, dict):
                    prompt_tokens = usage.get("prompt_token_count")
                    candidate_tokens = usage.get("candidates_token_count")
                    total_tokens = usage.get("total_token_count")
                else:
                    prompt_tokens = getattr(usage, "prompt_token_count", None)
                    candidate_tokens = getattr(usage, "candidates_token_count", None)
                    total_tokens = getattr(usage, "total_token_count", None)

            _log_gemini_call(
                request_id=request_id,
                business_id=business_id,
                latency_ms=latency_ms,
                attempt=attempt,
                status="success",
                error_type=None,
                prompt_tokens=prompt_tokens,
                candidate_tokens=candidate_tokens,
                total_tokens=total_tokens,
            )
            return response, attempt, None
        except Exception as error:
            latency_ms = int((monotonic() - start) * 1000)
            category, is_transient, error_type = _classify_gemini_error(error)
            _log_gemini_call(
                request_id=request_id,
                business_id=business_id,
                latency_ms=latency_ms,
                attempt=attempt,
                status="error",
                error_type=error_type,
            )
            last_error = error
            if not is_transient or attempt >= MAX_RETRIES:
                return None, attempt, (category, error_type)
            delay = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
            import time as _time
            _time.sleep(delay)

    if last_error is not None:
        cat, _, err_t = _classify_gemini_error(last_error)
        return None, attempt, (cat, err_t)
    return None, attempt, ("unknown", "UnknownError")


def _log_gemini_call(request_id=None, business_id=None, latency_ms=0,
                     attempt=0, status="unknown", error_type=None,
                     prompt_tokens=None, candidate_tokens=None, total_tokens=None):
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

17. Si no conocés el nombre del cliente, pedilo.

18. Utilizá buscar_turnos_cliente para obtener sus turnos.

19. Si el cliente tiene varios turnos, NO elijas uno al azar.

20. Mostrá los turnos relevantes y preguntá cuál quiere
    cancelar o reprogramar.


============================================================
CANCELAR
============================================================

21. Nunca canceles un turno sin identificar primero
    exactamente cuál quiere cancelar el cliente.

22. Utilizá cancelar_turno solamente cuando tengas
    el ID exacto del turno que el cliente quiere cancelar.

23. Si la cancelación fue exitosa, confirmala claramente.

24. Si la cancelación falla, no digas que fue cancelado.


============================================================
REPROGRAMAR
============================================================

25. Para reprogramar necesitás:

    - ID del turno
    - nueva fecha
    - nuevo horario

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
        return "No hay recursos reservables en este momento.", session_id, public_token

    return "\n".join(
        f"- {resource['name']} (ID: {resource['id']})"
        for resource in resources
    )


def get_business_hours_prompt(business_id):
    """Genera los horarios semanales actuales desde la base de datos."""
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    day_names = [
        "Lunes", "Martes", "Miércoles", "Jueves",
        "Viernes", "Sábado", "Domingo",
    ]
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
            "fecha": {
                "type": "string",
                "description": (
                    "Fecha en formato YYYY-MM-DD."
                ),
            },
            "servicio": {
                "type": "string",
                "description": (
                    "Nombre opcional del servicio para filtrar por duración."
                ),
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

        "required": [
            "fecha"
        ],

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

            "nombre": {
                "type": "string",
                "description": "Nombre del cliente.",
            },

            "telefono": {
                "type": "string",
                "description": "Teléfono del cliente.",
            },

            "email": {
                "type": "string",
                "description": (
                    "Email del cliente, en formato válido. Preguntarlo "
                    "si no lo dio. Es opcional pero recomendado para "
                    "enviar la confirmación y recordatorio."
                ),
            },

            "servicio": {
                "type": "string",
                "description": "Nombre exacto de un servicio activo.",
            },

            "fecha": {
                "type": "string",
                "description": (
                    "Fecha en formato YYYY-MM-DD."
                ),
            },

            "hora": {
                "type": "string",
                "description": (
                    "Hora en formato HH:MM."
                ),
            },

            "resource_id": {
                "type": "integer",
                "description": (
                    "ID opcional del recurso reservable. "
                    "Si se especifica, reserva ese recurso específico. "
                    "Debe coincidir con un recurso de 'RECURSOS ACTIVOS'."
                ),
            },
        },

        "required": [
            "nombre",
            "telefono",
            "servicio",
            "fecha",
            "hora",
        ],

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
        "Debe utilizarse antes de cancelar o reprogramar "
        "un turno cuando sea necesario identificarlo."
    ),

    parameters_json_schema={
        "type": "object",

        "properties": {

            "nombre": {
                "type": "string",

                "description": (
                    "Nombre del cliente."
                ),
            },

            "telefono": {
                "type": "string",
                "description": "Teléfono con el que se registró el turno.",
            },
        },

        "required": [
            "nombre",
            "telefono",
        ],

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

                "description": (
                    "ID exacto del turno que el cliente "
                    "quiere cancelar."
                ),
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

        "required": [
            "appointment_id",
            "telefono",
        ],

        "additionalProperties": False,
    },
)


# Gemini receives the H5 secret explicitly; the service enforces it for new appointments.
cancelar_turno_declaration.parameters_json_schema["properties"]["management_token"] = {
    "type": "string",
    "description": "Token secreto devuelto al crear el turno; obligatorio para turnos nuevos.",
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

                "description": (
                    "ID exacto del turno que se quiere "
                    "reprogramar."
                ),
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
                "description": "Token secreto devuelto al crear el turno; obligatorio para turnos nuevos.",
            },

            "nueva_fecha": {
                "type": "string",

                "description": (
                    "Nueva fecha en formato YYYY-MM-DD."
                ),
            },

            "nueva_hora": {
                "type": "string",

                "description": (
                    "Nueva hora en formato HH:MM."
                ),
            },
        },

        "required": [
            "appointment_id",
            "telefono",
            "nueva_fecha",
            "nueva_hora",
        ],

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
            },
        },
        "required": ["motivo"],
        "additionalProperties": False,
    },
)


def _execute_solicitar_atencion_humana(arguments, business_id):
    """Ejecuta la herramienta de solicitud de atención humana."""
    # La sesión se marca como needs_human en la capa de persistencia
    # Aquí solo confirmamos la acción
    return {
        "success": True,
        "message": "Tu solicitud ha sido registrada. Una persona del negocio te contactará pronto."
    }


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

def execute_tool(name, arguments, business_id=None):
    if business_id is None:
        return {"success": False, "error": "Contexto de negocio inválido."}

    # ========================================================
    # CONSULTAR RECURSOS
    # ========================================================

    if name == "consultar_recursos":

        try:

            resources = get_resources_scoped(business_id, only_active=True)
            recursos = [
                {"id": r["id"], "nombre": r["name"]}
                for r in resources
            ]

            return {
                "success": True,
                "recursos": recursos,
            }

        except Exception as error:

            logger.exception(
                "Error consultando recursos."
            )

            return {
                "success": False,
                "error": "No se pudo consultar los recursos.",
            }


    # ========================================================
    # CONSULTAR DISPONIBILIDAD
    # ========================================================

    if name == "consultar_disponibilidad":

        fecha = arguments["fecha"]
        servicio = arguments.get("servicio")
        resource_id = arguments.get("resource_id")

        try:

            horarios = get_available_times(fecha, business_id, servicio, resource_id)

            return {
                "success": True,
                "fecha": fecha,
                "horarios_disponibles": horarios,
            }

        except Exception as error:

            logger.exception(
                "Error consultando disponibilidad."
            )

            return {
                "success": False,
                "error": "No se pudo consultar la disponibilidad.",
            }


    # ========================================================
    # RESERVAR TURNO
    # ========================================================

    if name == "reservar_turno":

        try:

            active_services_a = get_active_services_scoped(business_id)

            if arguments["servicio"] not in {
                row["name"] for row in active_services_a
            }:
                return {
                    "success": False,
                    "message": "El servicio solicitado no está disponible.",
                }

            # Validar resource_id si se provee
            resource_id = arguments.get("resource_id")
            if resource_id is not None:
                # Verificar que el recurso existe y está activo en este negocio
                resources = get_resources_scoped(business_id, only_active=True)
                resource_ids = {r["id"] for r in resources}
                if resource_id not in resource_ids:
                    return {
                        "success": False,
                        "message": "El recurso especificado no está disponible.",
                    }

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
            )


            if not resultado.get("success"):

                return {
                    "success": False,
                    "reason": resultado.get("reason"),
                    "message": "No se pudo reservar el turno solicitado.",
                }


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

                "message": (
                    "El turno fue reservado "
                    "correctamente."
                ),
            }
            if resultado.get("resource_id"):
                response["resource_id"] = resultado["resource_id"]
                resource = get_resource_scoped(resultado["resource_id"], business_id)
                if resource:
                    response["resource_nombre"] = resource["name"]

            return response


        except Exception as error:

            logger.exception(
                "Error creando reserva."
            )

            return {
                "success": False,
                "error": "No se pudo crear la reserva.",
            }


    # ========================================================
    # BUSCAR TURNOS DEL CLIENTE
    # ========================================================

    if name == "buscar_turnos_cliente":

        try:

            nombre = arguments["nombre"]

            telefono = arguments["telefono"]


            turnos = get_customer_appointments(
                customer_name=nombre,
                phone=telefono,
                business_id=business_id,
            )


            return {
                "success": True,
                "turnos": turnos,
            }


        except Exception as error:

            logger.exception(
                "Error buscando turnos del cliente."
            )

            return {
                "success": False,
                "error": "No se pudieron consultar los turnos.",
            }


    # ========================================================
    # CANCELAR TURNO
    # ========================================================

    if name == "cancelar_turno":

        try:

            appointment_id = arguments[
                "appointment_id"
            ]

            # El management_token es obligatorio para cancelar
            management_token = arguments.get("management_token")
            if not management_token:
                return {
                    "success": False,
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

                    "message": (
                        "El turno fue cancelado "
                        "correctamente."
                    ),
                }


            return {
                "success": False,

                "message": (
                    "No pudimos encontrar ese turno con los datos indicados. "
                    "Verificá tu nombre y teléfono e intentá nuevamente."
                ),
            }


        except Exception as error:

            logger.exception(
                "Error cancelando turno."
            )

            return {
                "success": False,
                "error": "No se pudo cancelar el turno.",
            }


    # ========================================================
    # REPROGRAMAR TURNO
    # ========================================================

    if name == "reprogramar_turno":

        try:

            appointment_id = arguments[
                "appointment_id"
            ]

            nueva_fecha = arguments[
                "nueva_fecha"
            ]

            nueva_hora = arguments[
                "nueva_hora"
            ]

            # El management_token es obligatorio para reprogramar
            management_token = arguments.get("management_token")
            if not management_token:
                return {
                    "success": False,
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

                    "message": (
                        "El turno fue reprogramado "
                        "correctamente."
                    ),
                }


            reason = resultado.get(
                "reason"
            )


            if reason == "occupied":

                return {
                    "success": False,

                    "reason": "occupied",

                    "message": (
                        "El nuevo horario "
                        "ya está ocupado."
                    ),
                }


            if reason == "invalid_time":

                return {
                    "success": False,

                    "reason": "invalid_time",

                    "message": (
                        "El horario solicitado "
                        "no pertenece al horario "
                        "de atención del negocio."
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


            return {
                "success": False,

                "message": (
                    "No se pudo reprogramar "
                    "el turno."
                ),
            }


        except Exception as error:

            logger.exception(
                "Error reprogramando turno."
            )

            return {
                "success": False,
                "error": "No se pudo reprogramar el turno.",
            }


    return {
        "success": True,
        "message": "Tu solicitud ha sido registrada. Una persona del negocio te contactará pronto."
    }


    # ========================================================
    # SOLICITAR ATENCIÓN HUMANA
    # ========================================================

    if name == "solicitar_atencion_humana":

        motivo = arguments.get("motivo", "El cliente solicita hablar con una persona.")

        try:
            return _execute_solicitar_atencion_humana(arguments, business_id)

        except Exception as error:

            logger.exception(
                "Error solicitando atención humana."
            )

            return {
                "success": False,
                "error": "No se pudo solicitar atención humana.",
            }


    # ========================================================
    # HERRAMIENTA DESCONOCIDA
    # ========================================================


# ============================================================
# CONSTRUIR HISTORIAL
# ============================================================

def build_contents(
    conversation,
    message,
):

    contents = []


    for item in conversation:

        role = item.get("role")

        content = item.get(
            "content",
            "",
        )


        if role == "assistant":

            role = "model"


        if role not in (
            "user",
            "model",
        ):

            logger.warning(
                "Rol desconocido descartado: %s",
                role,
            )

            continue


        contents.append(

            types.Content(

                role=role,

                parts=[
                    types.Part.from_text(
                        text=str(content)
                    )
                ],
            )
        )


    contents.append(

        types.Content(

            role="user",

            parts=[
                types.Part.from_text(
                    text=message
                )
            ],
        )
    )


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
            for part in (content.parts or []):
                if part.text:
                    total += len(part.text)
        if total <= max_total_chars:
            break
        contents.pop(0)

    return contents


# ============================================================
# FORMATEAR CONFIRMACIÓN DE RESERVA
# ============================================================

def format_reservation_confirmation(
    nombre,
    servicio,
    fecha,
    hora,
    resource_nombre=None,
):

    business_name = get_business_identity()["business_name"]

    try:

        fecha_obj = datetime.strptime(
            fecha,
            "%Y-%m-%d",
        )


        dias = [
            "lunes",
            "martes",
            "miércoles",
            "jueves",
            "viernes",
            "sábado",
            "domingo",
        ]


        dia_semana = dias[
            fecha_obj.weekday()
        ]


        fecha_formateada = fecha_obj.strftime(
            "%d/%m/%Y"
        )

        resource_text = f"\n🏟️ **{resource_nombre}**" if resource_nombre else ""

        return (
            f"¡Listo, {nombre}! Tu turno para "
            f"**{servicio}** quedó reservado correctamente.\n\n"
            f"📅 **{dia_semana} {fecha_formateada}**\n"
            f"🕐 **{hora} hs**{resource_text}\n\n"
            f"¡Te esperamos en **{business_name}**!"
        )


    except Exception:

        logger.exception(
            "Error formateando confirmación."
        )


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

def format_reservation_error(
    arguments,
    result,
):

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

        nombre = arguments.get(
            "nombre",
            "",
        )


        if nombre:

            return (
                f"Disculpá, {nombre}. "
                f"{message}"
            )


        return message


    return (
        "Disculpá, no pude completar "
        "la reserva. Por favor, "
        "intentá nuevamente."
    )


# ============================================================
# FORMATEAR CANCELACIÓN
# ============================================================

def format_cancellation_confirmation():

    return (
        "Listo. Tu turno fue cancelado correctamente. "
        "El horario quedó nuevamente disponible."
    )


# ============================================================
# FORMATEAR REPROGRAMACIÓN
# ============================================================

def format_reschedule_confirmation(
    fecha,
    hora,
):

    business_name = get_business_identity()["business_name"]

    try:

        fecha_obj = datetime.strptime(
            fecha,
            "%Y-%m-%d",
        )


        dias = [
            "lunes",
            "martes",
            "miércoles",
            "jueves",
            "viernes",
            "sábado",
            "domingo",
        ]


        dia_semana = dias[
            fecha_obj.weekday()
        ]


        fecha_formateada = fecha_obj.strftime(
            "%d/%m/%Y"
        )


        return (
            "Listo. Tu turno fue reprogramado correctamente.\n\n"
            f"📅 **{dia_semana} {fecha_formateada}**\n"
            f"🕐 **{hora} hs**\n\n"
            f"¡Te esperamos en **{business_name}**!"
        )


    except Exception:

        logger.exception(
            "Error formateando reprogramación."
        )


        return (
            "Listo. Tu turno fue reprogramado "
            f"para **{fecha} a las {hora} hs**."
        )


# ============================================================
# FORMATEAR TURNOS
# ============================================================

def format_customer_appointments(
    turnos,
):

    if not turnos:

        return (
            "⚠️ No encontramos turnos con esos datos. Verificá que el "
            "nombre y el teléfono estén escritos correctamente e intentá nuevamente."
        )


    lines = [
        "Estos son tus turnos confirmados:"
    ]


    dias = [
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    ]


    for turno in turnos:

        try:

            fecha_obj = datetime.strptime(
                turno["appointment_date"],
                "%Y-%m-%d",
            )


            dia = dias[
                fecha_obj.weekday()
            ]


            fecha = fecha_obj.strftime(
                "%d/%m/%Y"
            )


        except Exception:

            dia = ""

            fecha = turno[
                "appointment_date"
            ]


        lines.append(

            f"- **{turno['service']}** — "
            f"{dia} {fecha} a las "
            f"**{turno['appointment_time']} hs**"
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
    public_token = None
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
        session = get_or_create_public_conversation_session_scoped(business_id)
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


    current_date = now.strftime(
        "%Y-%m-%d"
    )


    current_day = now.strftime(
        "%A"
    )


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
            knowledge_lines.append(f"[INFO NEGOCIO] Pregunta: {entry['question']}\nRespuesta: {entry['answer']}")
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

    contents = build_contents(
        conversation,
        message,
    )
    contents = truncate_history_by_total_chars(contents)


    # ========================================================
    # CONFIGURACIÓN GEMINI
    # ========================================================

    config = types.GenerateContentConfig(

        system_instruction=instructions,

        tools=[
            BARBERIA_TOOL
        ],

        temperature=0.2,

        max_output_tokens=MAX_OUTPUT_TOKENS,

        automatic_function_calling=(
            types.AutomaticFunctionCallingConfig(
                disable=True
            )
        ),
    )


    # ========================================================
    # PRIMERA LLAMADA
    # ========================================================

    response, attempt, error_info = _call_gemini_with_retry(
        contents, config, request_id=request_id, business_id=business_id
    )

    if response is None:
        category, error_type = error_info or ("unknown", "UnknownError")
        logger.error(
            "Gemini falló tras %d intento(s): category=%s error_type=%s",
            attempt, category, error_type,
        )
        return _gemini_error_message(category), session_id, public_token

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

            logger.error(
                "Se alcanzó el límite de iteraciones."
            )

            return (
                "Disculpá, estoy teniendo dificultades "
                "para resolver tu consulta. "
                "Por favor, intentá nuevamente.",
                session_id,
                public_token
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

                        function_calls.append(
                            part.function_call
                        )


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

                logger.exception(
                    "No se pudo obtener texto de Gemini."
                )

                return (
                    "Disculpá, no pude generar "
                    "una respuesta.",
                    session_id,
                    public_token
                )


        # ----------------------------------------------------
        # GUARDAR RESPUESTA DEL MODELO
        # ----------------------------------------------------

        if response.candidates:

            contents.append(
                response.candidates[0].content
            )


        # ====================================================
        # EJECUTAR HERRAMIENTAS
        # ====================================================

        function_response_parts = []


        for function_call in function_calls:

            tool_name = function_call.name


            arguments = dict(
                function_call.args or {}
            )


            logger.info("Herramienta solicitada: %s", tool_name)


            # ------------------------------------------------
            # EJECUTAR
            # ------------------------------------------------

            try:

                result = execute_tool(
                    tool_name,
                    arguments,
                    business_id,
                )

            except Exception as error:

                logger.exception(
                    "Error ejecutando herramienta."
                )

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

            if (
                tool_name == "reservar_turno"
                and result.get("success") is True
            ):

                confirmed_mutations.add("reservar_turno")

                logger.info(
                    "RESERVA CONFIRMADA. "
                    "No se realiza una segunda llamada a Gemini."
                )


                return format_reservation_confirmation(
                    nombre=arguments["nombre"],
                    servicio=arguments["servicio"],
                    fecha=arguments["fecha"],
                    hora=arguments["hora"],
                    resource_nombre=result.get("resource_nombre"),
                ), session_id, public_token


            # =================================================
            # RESERVA FALLIDA
            # =================================================

            if (
                tool_name == "reservar_turno"
                and result.get("success") is False
            ):

                return format_reservation_error(
                    arguments,
                    result,
                ), session_id, public_token


            # =================================================
            # CANCELACIÓN CONFIRMADA
            # =================================================

            if (
                tool_name == "cancelar_turno"
                and result.get("success") is True
            ):

                confirmed_mutations.add("cancelar_turno")

                logger.info(
                    "CANCELACIÓN CONFIRMADA."
                )


                return format_cancellation_confirmation(), session_id, public_token


            if (
                tool_name == "cancelar_turno"
                and result.get("success") is False
            ):

                logger.info(
                    "CANCELACIÓN FALLIDA."
                )

                msg = result.get("message")
                if msg:
                    return f"Disculpá, {msg}", session_id, public_token
                return (
                    "Disculpá, no se pudo cancelar el turno. "
                    "Verificá los datos e intentá nuevamente.",
                    session_id,
                    public_token
                )


            # =================================================
            # REPROGRAMACIÓN CONFIRMADA
            # =================================================

            if (
                tool_name == "reprogramar_turno"
                and result.get("success") is True
            ):

                confirmed_mutations.add("reprogramar_turno")

                logger.info(
                    "REPROGRAMACIÓN CONFIRMADA."
                )


                return format_reschedule_confirmation(
                    fecha=result["nueva_fecha"],
                    hora=result["nueva_hora"],
                ), session_id, public_token


            if (
                tool_name == "reprogramar_turno"
                and result.get("success") is False
            ):

                logger.info(
                    "REPROGRAMACIÓN FALLIDA."
                )

                msg = result.get("message")
                if msg:
                    return f"Disculpá, {msg}", session_id, public_token
                return (
                    "Disculpá, no se pudo reprogramar el turno. "
                    "Verificá los datos e intentá nuevamente.",
                    session_id,
                    public_token
                )


            # =================================================
            # RESPUESTAS DIRECTAS DE BÚSQUEDA
            #
            # En este punto dejamos que Gemini utilice
            # los resultados para continuar la conversación.
            # =================================================

            if (
                tool_name in _MUTATING_TOOLS
                and not isinstance(result.get("success"), bool)
            ):
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
                    public_token
                )

            function_response_parts.append(

                types.Part.from_function_response(

                    name=tool_name,

                    response={
                        "result": result
                    },
                )
            )


        # ====================================================
        # DEVOLVER RESULTADOS A GEMINI
        # ====================================================

        if function_response_parts:

            contents.append(

                types.Content(

                    role="user",

                    parts=function_response_parts,
                )
            )


        # ====================================================
        # CONTINUAR CON GEMINI
        # ====================================================

        response, attempt, error_info = _call_gemini_with_retry(
            contents, config, request_id=request_id, business_id=business_id
        )

        if response is None:
            category, error_type = error_info or ("unknown", "UnknownError")
            logger.error(
                "Gemini falló durante tool-calling loop tras %d intento(s): category=%s error_type=%s",
                attempt, category, error_type,
            )
            return _gemini_error_message(category), session_id, public_token


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

    print(
        "PRUEBA DEL RECEPCIONISTA IA"
    )

    print("=" * 60)

    print()


    pregunta = input(
        "Cliente: "
    )


    respuesta = ask_ai(
        pregunta
    )


    print()

    print(
        "RECEPCIONISTA:"
    )

    print(
        respuesta
    )

    print()

    print(
        "=" * 60
    )
