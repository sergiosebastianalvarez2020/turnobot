import os
import logging
from logging.handlers import RotatingFileHandler
import secrets
from collections import defaultdict, deque
from functools import wraps
from time import monotonic

from dotenv import load_dotenv
from flask import Flask, render_template, render_template_string, request, jsonify, session, redirect, url_for
from werkzeug.security import check_password_hash

from services.ai import ask_ai
from database.database import get_business_settings, get_connection, init_database, update_appointment_status

from services.appointments import (
    get_available_times,
    create_appointment,
    get_customer_appointments,
    cancel_appointment,
    reschedule_appointment,
    get_appointments,
    get_appointment_counts,
)


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log_dir = os.getenv("LOG_DIR", "logs")
os.makedirs(log_dir, exist_ok=True)
file_handler = RotatingFileHandler(
    os.path.join(log_dir, "app.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().addHandler(file_handler)

logger = logging.getLogger("el_corte.web")

if os.getenv("FLASK_ENV") == "production" and not os.getenv("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY es obligatoria en producción")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if os.getenv("FLASK_ENV") == "production" and ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD fue eliminado; use ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH:
    logger.warning("ADMIN_PASSWORD_HASH no está configurada: acceso administrativo deshabilitado")
if os.getenv("FLASK_ENV") == "production" and (not ADMIN_PASSWORD_HASH or os.getenv("COOKIE_SECURE") != "1"):
    raise RuntimeError("ADMIN_PASSWORD_HASH es obligatoria en producción")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)

init_database()

MAX_MESSAGE_LENGTH = 1_000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_LENGTH = 2_000
CHAT_REQUEST_LIMIT = 20
API_REQUEST_LIMIT = 60
RATE_LIMIT_WINDOW_SECONDS = 60

rate_limit_state = defaultdict(deque)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if os.getenv("FLASK_ENV") == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf_token(value):
    return bool(value) and secrets.compare_digest(value, session.get("csrf_token", ""))


app.jinja_env.globals["csrf_token"] = csrf_token


def _is_request_allowed(key, limit):
    now = monotonic()
    requests = rate_limit_state[key]
    while requests and now - requests[0] > RATE_LIMIT_WINDOW_SECONDS:
        requests.popleft()
    if len(requests) >= limit:
        return False
    requests.append(now)
    return True


def get_client_ip():
    return request.remote_addr or "unknown"


def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def is_chat_request_allowed(client_ip):
    return _is_request_allowed(f"chat:{client_ip}", CHAT_REQUEST_LIMIT)


def is_api_request_allowed(client_ip):
    return _is_request_allowed(f"api:{client_ip}", API_REQUEST_LIMIT)


def is_login_request_allowed(client_ip):
    return _is_request_allowed(f"login:{client_ip}", 10)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


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
        logger.exception("Error obteniendo servicios activos")
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


@app.route("/health", methods=["GET"])
def health():
    try:
        connection = get_connection()
        connection.execute("SELECT 1").fetchone()
        connection.close()
        return jsonify({"status": "ok", "database": "ok"})
    except Exception:
        logger.exception("Health check failed")
        return jsonify({"status": "error", "database": "error"}), 503


# ============================================================
# LOGIN / LOGOUT ADMIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("admin"):
            return redirect(url_for("admin"))
        return render_template("login.html", error=False)

    if not valid_csrf_token(request.form.get("csrf_token")):
        return render_template("login.html", error=True, error_message="La sesión expiró. Intentá nuevamente."), 400

    password = request.form.get("password", "")
    if not is_login_request_allowed(get_client_ip()):
        return render_template("login.html", error=True, error_message="Demasiados intentos. Esperá unos minutos."), 429
    authenticated = bool(ADMIN_PASSWORD_HASH and check_password_hash(ADMIN_PASSWORD_HASH, password))

    if authenticated:
        session["admin"] = True
        return redirect(url_for("admin"))

    return render_template("login.html", error=True)


@app.route("/logout", methods=["POST"])
def logout():
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin():
    settings = get_business_settings()
    status = request.args.get("status") or "confirmed"
    if status not in {"confirmed", "cancelled"}:
        status = "confirmed"
    appointment_date = request.args.get("fecha") or None
    return render_template(
        "admin.html",
        appointments=get_appointments(status=status, appointment_date=appointment_date),
        counts=get_appointment_counts(),
        selected_status=status,
        selected_date=appointment_date or "",
        business_name=settings["business_name"] if settings else "Mi negocio",
    )


@app.route("/admin/turnos/<int:appointment_id>/cancelar", methods=["POST"])
@admin_required
def admin_cancel_appointment(appointment_id):
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    update_appointment_status(appointment_id, "cancelled")
    return redirect(url_for("admin", status="confirmed"))


@app.route("/admin/turnos/<int:appointment_id>/estado", methods=["POST"])
@admin_required
def admin_update_appointment_status(appointment_id):
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Solicitud no válida", 400
    status = request.form.get("status", "")
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return "Estado no válido", 400
    update_appointment_status(appointment_id, status)
    return redirect(url_for("admin", status=status if status in {"confirmed", "cancelled"} else "confirmed"))


# ============================================================
# CHAT IA
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "El formato enviado no es válido."}), 400

        if not is_chat_request_allowed(get_client_ip()):
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

        logger.exception("Error procesando /chat")

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

    if not is_api_request_allowed(get_client_ip()):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429

    try:

        horarios = get_available_times(fecha)

        return jsonify({
            "success": True,
            "fecha": fecha,
            "horarios_disponibles": horarios
        })


    except Exception as error:

        logger.exception("Error consultando disponibilidad")

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

    if not is_api_request_allowed(get_client_ip()):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Esperá un momento."}), 429

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

        logger.exception("Error buscando turnos")

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

    if not is_api_request_allowed(get_client_ip()):
        return jsonify({
            "success": False,
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        nombre = data.get("nombre", "")
        telefono = data.get("telefono", "")
        if not isinstance(nombre, str) or not isinstance(telefono, str):
            return jsonify({"success": False, "error": "Nombre y teléfono deben ser texto."}), 400
        nombre = nombre.strip()

        telefono = telefono.strip()

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
            }), 201

        if resultado.get("reason") == "past_time":
            return jsonify({
                "success": False,
                "reason": "past_time",
                "error": "Ese horario ya pasó.",
            }), 400

        if resultado.get("reason") == "invalid_service":
            return jsonify({
                "success": False,
                "reason": "invalid_service",
                "error": "El servicio seleccionado no está disponible.",
            }), 400


        # ----------------------------------------------------
        # ERROR DESCONOCIDO
        # ----------------------------------------------------

        return jsonify({
            "success": False,
            "error": "No se pudo realizar la reserva."
        }), 500


    except Exception as error:

        logger.exception("Error reservando turno")

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

    if not is_api_request_allowed(get_client_ip()):
        return jsonify({
            "success": False,
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

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
            }), 400


        return jsonify({

            "success": True,

            "appointment_id": appointment_id,

            "message": "El turno fue cancelado correctamente."
        })


    except Exception as error:

        logger.exception("Error cancelando turno")

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

    if not is_api_request_allowed(get_client_ip()):
        return jsonify({
            "success": False,
            "error": "Demasiadas solicitudes. Esperá un momento."
        }), 429

    try:

        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400


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
            }), 400

        if resultado.get("reason") == "past_time":
            return jsonify({
                "success": False,
                "reason": "past_time",
                "error": "Ese horario ya pasó.",
            }), 400
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

        logger.exception("Error reprogramando turno")

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
