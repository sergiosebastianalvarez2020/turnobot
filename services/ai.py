import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
)
from database.database import (
    get_active_services,
    get_business_settings,
    get_weekly_schedule,
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


MODEL = "gemini-3.6-flash"

TIMEZONE = ZoneInfo(
    "America/Argentina/Buenos_Aires"
)

MAX_TOOL_ITERATIONS = 5


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

15. Si una pregunta no tiene relación con la barbería,
    explicá amablemente que solamente podés ayudar
    con servicios y turnos.


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


def get_services_prompt():
    """Genera la lista actual de servicios desde la base de datos."""
    services = get_active_services()
    if not services:
        return "No hay servicios habilitados en este momento."

    return "\n".join(
        f"- {service['name']}: ${service['price']:,.0f}".replace(",", ".") + " "
        f"({service['duration']} min)"
        for service in services
    )


def get_business_hours_prompt():
    """Genera los horarios semanales actuales desde la base de datos."""
    day_names = [
        "Lunes", "Martes", "Miércoles", "Jueves",
        "Viernes", "Sábado", "Domingo",
    ]
    lines = []
    for day, name in enumerate(day_names):
        schedule = get_weekly_schedule(day)
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
        "Consulta los horarios disponibles de la barbería "
        "para una fecha determinada."
    ),

    parameters_json_schema={
        "type": "object",

        "properties": {
            "fecha": {
                "type": "string",
                "description": (
                    "Fecha en formato YYYY-MM-DD."
                ),
            }
        },

        "required": [
            "fecha"
        ],

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
        },

        "required": [
            "appointment_id",
            "telefono",
        ],

        "additionalProperties": False,
    },
)


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
# TOOL DE GEMINI
# ============================================================

BARBERIA_TOOL = types.Tool(
    function_declarations=[
        consultar_disponibilidad_declaration,
        reservar_turno_declaration,
        buscar_turnos_declaration,
        cancelar_turno_declaration,
        reprogramar_turno_declaration,
    ]
)


# ============================================================
# EJECUCIÓN DE HERRAMIENTAS
# ============================================================

def execute_tool(name, arguments):

    # ========================================================
    # CONSULTAR DISPONIBILIDAD
    # ========================================================

    if name == "consultar_disponibilidad":

        fecha = arguments["fecha"]

        try:

            horarios = get_available_times(fecha)

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

            if arguments["servicio"] not in {
                row["name"] for row in get_active_services()
            }:
                return {
                    "success": False,
                    "message": "El servicio solicitado no está disponible.",
                }

            resultado = create_appointment(

                customer_name=arguments["nombre"],

                phone=arguments["telefono"],

                service=arguments["servicio"],

                appointment_date=arguments["fecha"],

                appointment_time=arguments["hora"],
            )


            if not resultado.get("success"):

                return {
                    "success": False,
                    "reason": resultado.get("reason"),
                    "message": "No se pudo reservar el turno solicitado.",
                }


            return {
                "success": True,

                "appointment_id": resultado["appointment_id"],

                "message": (
                    "El turno fue reservado "
                    "correctamente."
                ),
            }


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


            resultado = cancel_appointment(
                appointment_id,
                arguments["telefono"],
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


            resultado = reschedule_appointment(

                appointment_id=appointment_id,

                new_date=nueva_fecha,

                new_time=nueva_hora,

                phone=arguments["telefono"],
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
                        "de atención de la barbería."
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


    # ========================================================
    # HERRAMIENTA DESCONOCIDA
    # ========================================================

    logger.warning(
        "Herramienta desconocida: %s",
        name,
    )

    return {
        "success": False,

        "error": (
            f"Herramienta desconocida: {name}"
        ),
    }


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


# ============================================================
# FORMATEAR CONFIRMACIÓN DE RESERVA
# ============================================================

def format_reservation_confirmation(
    nombre,
    servicio,
    fecha,
    hora,
):

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
            f"¡Listo, {nombre}! Tu turno para "
            f"**{servicio}** quedó reservado correctamente.\n\n"
            f"📅 **{dia_semana} {fecha_formateada}**\n"
            f"🕐 **{hora} hs**\n\n"
            f"¡Te esperamos en **El Corte**!"
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
            f"¡Te esperamos en **El Corte**!"
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
        "closed_day": "Ese día la barbería está cerrada.",
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
            "¡Te esperamos en **El Corte**!"
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
):

    if conversation is None:

        conversation = []


    # ========================================================
    # FECHA ACTUAL
    # ========================================================

    now = datetime.now(
        TIMEZONE
    )


    current_date = now.strftime(
        "%Y-%m-%d"
    )


    current_day = now.strftime(
        "%A"
    )


    # ========================================================
    # INSTRUCCIONES
    # ========================================================

    services_text = get_services_prompt()
    business_hours_text = get_business_hours_prompt()
    settings = get_business_settings()
    business_name = settings["business_name"] if settings else "Mi negocio"

    instructions = f"""
{SYSTEM_PROMPT}

============================================================
NOMBRE DEL NEGOCIO
============================================================

{business_name}

============================================================
SERVICIOS ACTUALES
============================================================

{services_text}

============================================================
HORARIOS ACTUALES
============================================================

{business_hours_text}

============================================================
FECHA ACTUAL
============================================================

{current_date}

DÍA ACTUAL:

{current_day}

ZONA HORARIA:

America/Argentina/Buenos_Aires

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


    # ========================================================
    # CONFIGURACIÓN GEMINI
    # ========================================================

    config = types.GenerateContentConfig(

        system_instruction=instructions,

        tools=[
            BARBERIA_TOOL
        ],

        temperature=0.2,

        automatic_function_calling=(
            types.AutomaticFunctionCallingConfig(
                disable=True
            )
        ),
    )


    # ========================================================
    # PRIMERA LLAMADA
    # ========================================================

    try:

        response = get_gemini_client().models.generate_content(

            model=MODEL,

            contents=contents,

            config=config,
        )


    except Exception:

        logger.exception(
            "Error llamando a Gemini."
        )

        return (
            "Disculpá, en este momento estoy teniendo "
            "un problema para procesar tu consulta."
        )


    # ========================================================
    # CICLO DE HERRAMIENTAS
    # ========================================================

    iteration = 0


    while True:

        iteration += 1


        if iteration > MAX_TOOL_ITERATIONS:

            logger.error(
                "Se alcanzó el límite de iteraciones."
            )

            return (
                "Disculpá, estoy teniendo dificultades "
                "para resolver tu consulta. "
                "Por favor, intentá nuevamente."
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

                return response.text

            except Exception:

                logger.exception(
                    "No se pudo obtener texto de Gemini."
                )

                return (
                    "Disculpá, no pude generar "
                    "una respuesta."
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
                return format_customer_appointments([])


            # =================================================
            # RESERVA CONFIRMADA
            # =================================================

            if (
                tool_name == "reservar_turno"
                and result.get("success") is True
            ):

                logger.info(
                    "RESERVA CONFIRMADA. "
                    "No se realiza una segunda llamada a Gemini."
                )


                return format_reservation_confirmation(

                    nombre=arguments["nombre"],

                    servicio=arguments["servicio"],

                    fecha=arguments["fecha"],

                    hora=arguments["hora"],
                )


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
                )


            # =================================================
            # CANCELACIÓN CONFIRMADA
            # =================================================

            if (
                tool_name == "cancelar_turno"
                and result.get("success") is True
            ):

                logger.info(
                    "CANCELACIÓN CONFIRMADA."
                )


                return format_cancellation_confirmation()


            # =================================================
            # REPROGRAMACIÓN CONFIRMADA
            # =================================================

            if (
                tool_name == "reprogramar_turno"
                and result.get("success") is True
            ):

                logger.info(
                    "REPROGRAMACIÓN CONFIRMADA."
                )


                return format_reschedule_confirmation(

                    fecha=result["nueva_fecha"],

                    hora=result["nueva_hora"],
                )


            # =================================================
            # RESPUESTAS DIRECTAS DE BÚSQUEDA
            #
            # En este punto dejamos que Gemini utilice
            # los resultados para continuar la conversación.
            # =================================================

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

        try:

            response = get_gemini_client().models.generate_content(

                model=MODEL,

                contents=contents,

                config=config,
            )


        except Exception:

            logger.exception(
                "Error llamando a Gemini durante continuación."
            )


            return (
                "Disculpá, ocurrió un problema al "
                "procesar la información. "
                "Por favor, intentá nuevamente."
            )


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
