from collections import defaultdict, deque
from time import monotonic

from flask import Flask, render_template, request, jsonify

from services.ai import ask_ai
from database.database import get_business_settings, get_connection, init_database

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
    get_appointments,
)


app = Flask(__name__)
init_database()

MAX_MESSAGE_LENGTH = 1_000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_LENGTH = 2_000
CHAT_REQUEST_LIMIT = 20
CHAT_REQUEST_WINDOW_SECONDS = 60
chat_requests = defaultdict(deque)


def is_chat_request_allowed(client_ip):
    """Limita consultas de chat por IP para evitar abuso de la API de IA."""
    now = monotonic()
    requests = chat_requests[client_ip]
    while requests and now - requests[0] > CHAT_REQUEST_WINDOW_SECONDS:
        requests.popleft()
    if len(requests) >= CHAT_REQUEST_LIMIT:
        return False
    requests.append(now)
    return True


# ============================================================
# CONFIGURACIÓN DEL NEGOCIO
# ============================================================

def get_active_services():
    """
    Obtiene los servicios activos desde la base de datos.
    Siempre consulta BD para obtener configuración en vivo.
    """
    from database.database import get_active_services as db_get_active_services

    try:
        rows = db_get_active_services()
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
        return {}


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================

@app.route("/")
def index():
    settings = get_business_settings()
    return render_template(
        "index.html",
        business_name=settings["business_name"] if settings else "Mi negocio",
    )


@app.route("/admin")
def admin():
    settings = get_business_settings()
    return render_template(
        "admin.html",
        appointments=get_appointments(),
        business_name=settings["business_name"] if settings else "Mi negocio",
    )


# ============================================================
# CHAT IA
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():

    try:

        data = request.get_json(silent=True) or {}

        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "El formato enviado no es válido."}), 400

        if not is_chat_request_allowed(request.remote_addr or "unknown"):
            return jsonify({
                "success": False,
                "error": "Esperá un momento antes de enviar otro mensaje."
            }), 429

        raw_message = data.get("message", "")
        if not isinstance(raw_message, str):
            return jsonify({"success": False, "error": "El mensaje debe ser texto."}), 400

        message = raw_message.strip()

        conversation = data.get(
            "conversation",
            []
        )

        if not isinstance(conversation, list) or len(conversation) > MAX_HISTORY_MESSAGES:
            return jsonify({"success": False, "error": "El historial no es válido."}), 400

        if any(
            not isinstance(item, dict)
            or item.get("role") not in ("user", "assistant")
            or not isinstance(item.get("content"), str)
            or len(item["content"]) > MAX_HISTORY_CONTENT_LENGTH
            for item in conversation
        ):
            return jsonify({"success": False, "error": "El historial no es válido."}), 400

        if not message:

            return jsonify({
                "success": False,
                "error": "No se recibió ningún mensaje."
            }), 400

        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({
                "success": False,
                "error": "El mensaje es demasiado largo."
            }), 400


        response = ask_ai(
            message,
            conversation
        )


        return jsonify({
            "success": True,
            "response": response
        })


    except Exception as error:

        print("ERROR EN /chat:")
        print(error)

        return jsonify({
            "success": False,
            "error": "No se pudo procesar la consulta."
        }), 500


# ============================================================
# API - SERVICIOS
# ============================================================

@app.route("/api/servicios", methods=["GET"])
def api_servicios():

    services = get_active_services()

    servicios = []

    for nombre, info in services.items():

        servicios.append({
            "nombre": nombre,
            "precio": info["price"],
            "duracion": info["duration"]
        })


    return jsonify({
        "success": True,
        "servicios": servicios
    })


# ============================================================
# API - DISPONIBILIDAD
# ============================================================

@app.route(
    "/api/disponibilidad/<fecha>",
    methods=["GET"]
)
def api_disponibilidad(fecha):

    try:

        horarios = get_available_times(fecha)

        return jsonify({
            "success": True,
            "fecha": fecha,
            "horarios_disponibles": horarios
        })


    except Exception as error:

        print("ERROR DISPONIBILIDAD:")
        print(error)

        return jsonify({
            "success": False,
            "error": "No se pudo consultar la disponibilidad."
        }), 500


# ============================================================
# API - BUSCAR TURNOS
# ============================================================

@app.route(
    "/api/turnos",
    methods=["GET"]
)
def api_turnos():

    nombre = request.args.get(
        "nombre",
        ""
    ).strip()

    telefono = request.args.get("telefono", "").strip()


    if not nombre:

        return jsonify({
            "success": False,
            "error": "El nombre es obligatorio."
        }), 400

    if not telefono:
        return jsonify({
            "success": False,
            "error": "El teléfono es obligatorio para consultar tus turnos."
        }), 400


    try:

        turnos = get_customer_appointments(
            nombre,
            telefono
        )


        return jsonify({
            "success": True,
            "turnos": turnos
        })


    except Exception as error:

        print("ERROR BUSCANDO TURNOS:")
        print(error)

        return jsonify({
            "success": False,
            "error": "No se pudieron consultar los turnos."
        }), 500


# ============================================================
# API - RESERVAR
# ============================================================

@app.route(
    "/api/reservar",
    methods=["POST"]
)
def api_reservar():

    try:

        data = request.get_json(silent=True) or {}

        nombre = data.get(
            "nombre",
            ""
        ).strip()

        telefono = data.get(
            "telefono",
            ""
        ).strip()

        servicio = data.get(
            "servicio"
        )

        fecha = data.get(
            "fecha"
        )

        hora = data.get(
            "hora"
        )


        # ----------------------------------------------------
        # VALIDACIONES
        # ----------------------------------------------------

        if not nombre:

            return jsonify({
                "success": False,
                "error": "El nombre y apellido son obligatorios."
            }), 400


        if not telefono:

            return jsonify({
                "success": False,
                "error": "El teléfono es obligatorio."
            }), 400


        services = get_active_services()

        if servicio not in services:

            return jsonify({
                "success": False,
                "error": "El servicio seleccionado no es válido."
            }), 400


        if not fecha or not hora:

            return jsonify({
                "success": False,
                "error": "La fecha y el horario son obligatorios."
            }), 400


        # ----------------------------------------------------
        # CREAR TURNO
        # ----------------------------------------------------

        resultado = create_appointment(

            customer_name=nombre,

            phone=telefono,

            service=servicio,

            appointment_date=fecha,

            appointment_time=hora,
        )


        # ----------------------------------------------------
        # FECHA PASADA
        # ----------------------------------------------------

        if resultado.get("reason") == "past_date":

            return jsonify({
                "success": False,
                "reason": "past_date",
                "error": "No podés reservar una fecha que ya pasó."
            }), 400


        # ----------------------------------------------------
        # DÍA CERRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "closed_day":

            return jsonify({
                "success": False,
                "reason": "closed_day",
                "error": "Ese día estamos cerrados."
            }), 400


        # ----------------------------------------------------
        # FECHA INVÁLIDA
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_date":

            return jsonify({
                "success": False,
                "reason": "invalid_date",
                "error": "La fecha seleccionada no es válida."
            }), 400


        # ----------------------------------------------------
        # HORARIO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_time":

            return jsonify({
                "success": False,
                "reason": "invalid_time",
                "error": "El horario seleccionado no es válido."
            }), 400


        # ----------------------------------------------------
        # HORARIO OCUPADO
        # ----------------------------------------------------

        if resultado.get("reason") == "occupied":

            return jsonify({
                "success": False,
                "reason": "occupied",
                "error": "Ese horario ya está ocupado."
            }), 400


        # ----------------------------------------------------
        # RESERVA CORRECTA
        # ----------------------------------------------------

        if resultado.get("success"):

            return jsonify({

                "success": True,

                "appointment_id": resultado.get(
                    "appointment_id"
                ),

                "message": "El turno fue reservado correctamente."
            })


        # ----------------------------------------------------
        # ERROR DESCONOCIDO
        # ----------------------------------------------------

        return jsonify({
            "success": False,
            "error": "No se pudo realizar la reserva."
        }), 500


    except Exception as error:

        print("ERROR RESERVANDO:")
        print(error)

        return jsonify({
            "success": False,
            "error": "No se pudo realizar la reserva."
        }), 500


# ============================================================
# API - CANCELAR
# ============================================================

@app.route(
    "/api/cancelar",
    methods=["POST"]
)
def api_cancelar():

    try:

        data = request.get_json(silent=True) or {}

        appointment_id = data.get(
            "appointment_id"
        )

        telefono = data.get("telefono", "").strip()


        if not appointment_id:

            return jsonify({
                "success": False,
                "error": "Falta el ID del turno."
            }), 400

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400


        resultado = cancel_appointment(
            appointment_id,
            telefono,
        )


        if not resultado:

            return jsonify({
                "success": False,
                "error": "No pudimos encontrar ese turno con los datos indicados. Verificá tu nombre y teléfono e intentá nuevamente."
            })


        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue cancelado correctamente."
        })


    except Exception as error:

        print("ERROR CANCELANDO:")
        print(error)

        return jsonify({
            "success": False,
            "error": "No se pudo cancelar el turno."
        }), 500


# ============================================================
# API - REPROGRAMAR
# ============================================================

@app.route(
    "/api/reprogramar",
    methods=["POST"]
)
def api_reprogramar():

    try:

        data = request.get_json(silent=True) or {}


        appointment_id = data.get(
            "appointment_id"
        )

        nueva_fecha = data.get(
            "nueva_fecha"
        )

        nueva_hora = data.get(
            "nueva_hora"
        )

        telefono = data.get("telefono", "").strip()


        if not appointment_id:

            return jsonify({
                "success": False,
                "error": "Falta el ID del turno."
            }), 400


        if not nueva_fecha or not nueva_hora:

            return jsonify({
                "success": False,
                "error": "La nueva fecha y hora son obligatorias."
            }), 400

        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400


        resultado = reschedule_appointment(

            appointment_id,

            nueva_fecha,

            nueva_hora,

            telefono,
        )


        # ----------------------------------------------------
        # HORARIO OCUPADO
        # ----------------------------------------------------

        if resultado.get("reason") == "occupied":

            return jsonify({

                "success": False,

                "reason": "occupied",

                "error": "El nuevo horario ya está ocupado."
            })


        # ----------------------------------------------------
        # TURNO NO ENCONTRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "not_found":

            return jsonify({

                "success": False,

                "reason": "not_found",

                "error": "No pudimos encontrar ese turno con los datos indicados. Verificá tu nombre y teléfono e intentá nuevamente."
            })


        # ----------------------------------------------------
        # HORARIO INVÁLIDO
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_time":

            return jsonify({

                "success": False,

                "reason": "invalid_time",

                "error": "El horario seleccionado no es válido."
            })
                # ----------------------------------------------------
        # FECHA PASADA
        # ----------------------------------------------------

        if resultado.get("reason") == "past_date":

            return jsonify({

                "success": False,

                "reason": "past_date",

                "error": "No podés reprogramar el turno para una fecha que ya pasó."
            }), 400


        # ----------------------------------------------------
        # DÍA CERRADO
        # ----------------------------------------------------

        if resultado.get("reason") == "closed_day":

            return jsonify({

                "success": False,

                "reason": "closed_day",

                "error": "Ese día estamos cerrados."
            }), 400


        # ----------------------------------------------------
        # FECHA INVÁLIDA
        # ----------------------------------------------------

        if resultado.get("reason") == "invalid_date":

            return jsonify({

                "success": False,

                "reason": "invalid_date",

                "error": "La fecha seleccionada no es válida."
            }), 400


        # ----------------------------------------------------
        # CORRECTO
        # ----------------------------------------------------

        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue reprogramado correctamente."
        })


    except Exception as error:

        print("ERROR REPROGRAMANDO:")
        print(error)

        return jsonify({

            "success": False,

            "error": "No se pudo reprogramar el turno."
        }), 500


# ============================================================
# INICIAR SERVIDOR
# ============================================================

if __name__ == "__main__":

    app.run(

        debug=False,

        host="127.0.0.1",

        port=5000
    )
