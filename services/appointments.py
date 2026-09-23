import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if hasattr(re, "Pattern"):
    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
else:  # pragma: no cover
    _EMAIL_RE = None

from database.database import (
    get_active_services_scoped,
    get_business_settings_scoped,
    get_connection,
    get_resource_scoped,
    get_weekly_schedule_scoped,
)

DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


def _validate_resource(resource_id, business_id, connection=None):
    """Valida que `resource_id` pertenezca al negocio indicado.

    Devuelve (resource_id_validado | None, reason | None).

    - resource_id None -> (None, None): negocio sin recursos o reserva global.
    - resource_id inválido o de OTRO negocio -> (None, "invalid_resource").
    Nunca se confía en el id crudo recibido del cliente: siempre se comprueba
    su business_id en la tabla resources.
    """
    if resource_id is None:
        return None, None
    try:
        resource_id_int = int(resource_id)
        if resource_id_int <= 0:
            return None, "invalid_resource"
    except (ValueError, TypeError):
        return None, "invalid_resource"
    resource = get_resource_scoped(resource_id_int, business_id, connection=connection)
    if resource is None or not resource["active"]:
        return None, "invalid_resource"
    return resource_id_int, None


def _to_minutes(hm):
    """Convierte 'HH:MM' a minutos desde medianoche."""
    hour, minute = hm.split(":")
    return int(hour) * 60 + int(minute)


def _to_hhmm(minutes):
    """Convierte minutos desde medianoche a 'HH:MM' (con ceros a la izquierda)."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fits_closing(date, time, end_minutes, business_id, connection=None):
    """Devuelve True si un turno que comienza en `time` y termina en
    `end_minutes` cabe dentro del cierre del bloque (mañana/tarde) que lo
    contiene. Permite terminar exactamente al cierre."""
    appointment_date = datetime.strptime(date, "%Y-%m-%d").date()
    schedule = get_weekly_schedule_scoped(
        appointment_date.weekday(), business_id, connection=connection
    )
    if not schedule or not schedule["is_open"]:
        return False
    start_minutes = _to_minutes(time)
    for start, end in (
        (schedule["morning_start"], schedule["morning_end"]),
        (schedule["afternoon_start"], schedule["afternoon_end"]),
    ):
        if not start or not end:
            continue
        block_start = _to_minutes(start)
        block_end = _to_minutes(end)
        if block_start <= start_minutes < block_end:
            return end_minutes <= block_end
    return False


def get_business_timezone(business_id, connection=None):
    settings = get_business_settings_scoped(business_id, connection=connection)
    configured_timezone = (
        settings["timezone"] if settings and settings["timezone"] else DEFAULT_TIMEZONE
    )
    try:
        ZoneInfo(configured_timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return DEFAULT_TIMEZONE
    return configured_timezone


# ============================================================
# FUNCIONES DE VALIDACIÓN
# ============================================================


def validate_phone(phone):
    """
    Valida que el teléfono:
    - contenga solo dígitos
    - tenga al menos 7 dígitos

    Devuelve True si es válido, False si no.
    """
    if not phone or not isinstance(phone, str):
        return False

    phone_clean = normalize_phone(phone)

    if not phone_clean:
        return False

    # Verificar que tenga al menos 7 dígitos
    if len(phone_clean) < 7:
        return False

    return True


def normalize_phone(phone):
    """Normaliza teléfonos para aceptar espacios, guiones y prefijo +."""
    if not phone or not isinstance(phone, str):
        return ""
    phone_clean = re.sub(r"[\s()-]", "", phone.strip())
    if phone_clean.startswith("+"):
        phone_clean = phone_clean[1:]
    return phone_clean if phone_clean.isdigit() else ""


def validate_customer_name(customer_name):
    """
    Valida que el nombre del cliente:
    - sea un string
    - tenga al menos 2 caracteres

    Devuelve True si es válido, False si no.
    """
    if not customer_name or not isinstance(customer_name, str):
        return False

    # Remover espacios en blanco al inicio y final
    name_clean = customer_name.strip()

    # Verificar que tenga al menos 2 caracteres
    if len(name_clean) < 2:
        return False

    return True


def validate_email(email):
    """Valida un email. Devuelve True si es válido, False si no.

    Acepta None/vacío como valor no presente (para no forzar email en
    flujos donde no aplica). Un email no vacío debe tener formato básico.
    """
    if not email or not isinstance(email, str):
        return True
    cleaned = email.strip()
    if not cleaned:
        return True
    return bool(_EMAIL_RE.match(cleaned))


# ============================================================
# HORARIOS DISPONIBLES
# ============================================================


def get_available_slots(date, business_id, service=None, duration=None, connection=None):
    """Construye la grilla de horarios a partir de la configuración del negocio.

    slot_duration es la GRANULARIDAD de la grilla (paso entre comienzos),
    NO la duración de reserva. Un slot es válido si su comienzo más el
    servicio activo más largo cabe antes del cierre del bloque
    (mañana/tarde) correspondiente.

    break_between_slots se conserva como intervalo agregado al paso entre
    comienzos de slots (comportamiento histórico): el paso efectivo es
    slot_duration + break_between_slots.
    """
    appointment_date = datetime.strptime(date, "%Y-%m-%d").date()
    schedule = get_weekly_schedule_scoped(
        appointment_date.weekday(), business_id, connection=connection
    )
    settings = get_business_settings_scoped(business_id, connection=connection)

    if not schedule or not schedule["is_open"]:
        return []

    slot_duration = settings["slot_duration"] if settings and settings["slot_duration"] else 60
    break_between_slots = (
        settings["break_between_slots"] if settings and settings["break_between_slots"] else 0
    )
    services = get_active_services_scoped(business_id, connection=connection)
    if duration is not None and duration <= 0:
        return []
    if duration is not None:
        service_duration = duration
    elif service is None:
        service_duration = max((item["duration"] for item in services), default=slot_duration)
    else:
        selected = next((item for item in services if item["name"] == service), None)
        if selected is None or selected["duration"] <= 0:
            return []
        service_duration = selected["duration"]
    step = slot_duration + break_between_slots
    slots = []
    for start, end in (
        (schedule["morning_start"], schedule["morning_end"]),
        (schedule["afternoon_start"], schedule["afternoon_end"]),
    ):
        if not start or not end:
            continue
        opening = datetime.strptime(start, "%H:%M")
        closing = datetime.strptime(end, "%H:%M")
        current = opening
        while current < closing:
            if current + timedelta(minutes=service_duration) <= closing:
                slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=step)
    return slots


# ============================================================
# VALIDAR FECHA
# ============================================================


def validate_appointment_date(date, business_id, connection=None):
    """
    Valida que la fecha del turno:

    - tenga formato YYYY-MM-DD
    - no sea anterior a hoy
    - no sea domingo

    business_id es obligatorio: el horario se resuelve SIEMPRE con scope
    de negocio (nunca se cae al horario de otro tenant).

    Devuelve:

        {
            "valid": True,
            "reason": None
        }

    o:

        {
            "valid": False,
            "reason": "..."
        }
    """

    try:
        appointment_date = datetime.strptime(date, "%Y-%m-%d").date()

    except (ValueError, TypeError):
        return {"valid": False, "reason": "invalid_date"}

    today = datetime.now(ZoneInfo(get_business_timezone(business_id, connection=connection))).date()

    # --------------------------------------------------------
    # FECHA PASADA
    # --------------------------------------------------------

    if appointment_date < today:
        return {"valid": False, "reason": "past_date"}

    schedule = get_weekly_schedule_scoped(
        appointment_date.weekday(), business_id, connection=connection
    )

    if not schedule or not schedule["is_open"]:
        return {"valid": False, "reason": "closed_day"}

    return {"valid": True, "reason": None}


def validate_appointment_time(date, time, business_id=None, connection=None):
    """Valida HH:MM y evita reservar una hora pasada del día actual."""
    try:
        parsed_time = datetime.strptime(time, "%H:%M").time()
        appointment_date = datetime.strptime(date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, "invalid_time"

    now = datetime.now(ZoneInfo(get_business_timezone(business_id, connection=connection)))
    if appointment_date == now.date() and parsed_time <= now.time().replace(
        second=0, microsecond=0
    ):
        return False, "past_time"
    return True, None


# ============================================================
# CONSULTAR DISPONIBILIDAD
# ============================================================


def get_available_times(date, business_id=None, service=None, resource_id=None):
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    """
    Devuelve los horarios que todavía están libres
    para una determinada fecha.

    Si se solicita un resource_id, los horarios ocupados son los de ese
    recurso más los bloqueos globales (resource_id IS NULL) del negocio.
    Sin resource_id se conserva el comportamiento histórico: cualquier
    turno confirmado del negocio ocupa la grilla.

    Si la fecha no es válida, es pasada o es domingo,
    devuelve una lista vacía.
    """

    connection = get_connection()

    try:
        validation = validate_appointment_date(date, business_id, connection=connection)

        if not validation["valid"]:
            return []
        service_duration = None
        if service is not None:
            selected = next(
                (
                    row
                    for row in get_active_services_scoped(business_id, connection=connection)
                    if row["name"] == service
                ),
                None,
            )
            if selected is None or selected["duration"] <= 0:
                return []
            service_duration = selected["duration"]

        resource_id_validated, resource_reason = _validate_resource(
            resource_id, business_id, connection=connection
        )
        if resource_id is not None and resource_id_validated is None:
            return []

        if resource_id_validated is not None:
            # Disponibilidad por recurso: ocupan ese recurso y los bloqueos
            # globales (resource_id IS NULL) del negocio. Otros recursos NO.
            rows = connection.execute(
                """
                SELECT appointment_time, appointment_end
                FROM appointments
                WHERE appointment_date = ?
                AND status = 'confirmed'
                AND business_id = ?
                AND (resource_id = ? OR resource_id IS NULL)
                """,
                (date, business_id, resource_id_validated),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT appointment_time, appointment_end
                FROM appointments
                WHERE appointment_date = ?
                AND status = 'confirmed'
                AND business_id = ?
                """,
                (date, business_id),
            ).fetchall()

        occupied_intervals = [
            (_to_minutes(row["appointment_time"]), _to_minutes(row["appointment_end"]))
            for row in rows
        ]

        return [
            time
            for time in get_available_slots(date, business_id, service, connection=connection)
            if not any(
                _to_minutes(time) < end_minute
                and (
                    service_duration is None or _to_minutes(time) + service_duration > start_minute
                )
                for start_minute, end_minute in occupied_intervals
            )
        ]

    finally:
        connection.close()


# ============================================================
# CREAR TURNO
# ============================================================


def create_appointment(
    customer_name,
    phone,
    service,
    appointment_date,
    appointment_time,
    business_id,
    email=None,
    resource_id=None,
):
    """
    Crea un turno.

    Valida:

    - nombre válido (al menos 2 caracteres)
    - teléfono válido (solo dígitos, al menos 7)
    - email válido (si se provee)
    - fecha válida
    - fecha no pasada
    - día de atención
    - horario válido
    - horario disponible

    Devuelve SIEMPRE un diccionario.

    Éxito:

        {
            "success": True,
            "appointment_id": 123,
            "reason": "created",
            "customer_email": "cliente@ejemplo.com"
        }

    Error:

        {
            "success": False,
            "appointment_id": None,
            "reason": "invalid_name|invalid_phone|invalid_email|occupied|..."
        }
    """

    # --------------------------------------------------------
    # VALIDAR NOMBRE
    # --------------------------------------------------------

    if not validate_customer_name(customer_name):
        return {"success": False, "appointment_id": None, "reason": "invalid_name"}

    # --------------------------------------------------------
    # VALIDAR TELÉFONO
    # --------------------------------------------------------

    if not validate_phone(phone):
        return {"success": False, "appointment_id": None, "reason": "invalid_phone"}

    # --------------------------------------------------------
    # VALIDAR EMAIL (si se provee)
    # --------------------------------------------------------

    if not validate_email(email):
        return {"success": False, "appointment_id": None, "reason": "invalid_email"}
    customer_email = (email or "").strip()

    connection = get_connection()

    try:
        active_services = get_active_services_scoped(business_id, connection=connection)
        service_row = next((row for row in active_services if row["name"] == service), None)
        if service_row is None:
            return {"success": False, "appointment_id": None, "reason": "invalid_service"}

        duration = service_row["duration"]
        if duration is None or duration <= 0:
            return {"success": False, "appointment_id": None, "reason": "invalid_duration"}

        # --------------------------------------------------------
        # VALIDAR RECURSO (si se provee)
        #
        # El resource_id recibido del cliente se comprueba SIEMPRE contra el
        # business_id actual en la tabla resources. Un recurso de otro negocio
        # es rechazado con invalid_resource.
        # --------------------------------------------------------

        resource_id_validated, resource_reason = _validate_resource(
            resource_id, business_id, connection=connection
        )
        if resource_reason is not None:
            return {"success": False, "appointment_id": None, "reason": resource_reason}

        # --------------------------------------------------------
        # VALIDAR FECHA
        # --------------------------------------------------------

        validation = validate_appointment_date(appointment_date, business_id, connection=connection)

        if not validation["valid"]:
            return {"success": False, "appointment_id": None, "reason": validation["reason"]}

        # --------------------------------------------------------
        # VALIDAR HORARIO (formato y hora pasada)
        # --------------------------------------------------------

        valid_time, time_reason = validate_appointment_time(
            appointment_date, appointment_time, business_id, connection=connection
        )
        if not valid_time:
            return {"success": False, "appointment_id": None, "reason": time_reason}

        start_minutes = _to_minutes(appointment_time)
        end_minutes = start_minutes + duration
        if end_minutes <= start_minutes:
            return {"success": False, "appointment_id": None, "reason": "invalid_duration"}
        appointment_end = _to_hhmm(end_minutes)

        if appointment_time not in get_available_slots(
            appointment_date, business_id, service, connection=connection
        ):
            return {"success": False, "appointment_id": None, "reason": "invalid_time"}

        # Iniciar transacción atómica
        connection.execute("BEGIN IMMEDIATE")

        try:
            # ------------------------------------------------
            # VALIDAR HORARIO DE CIERRE
            #
            # El turno solo es válido si su fin no excede el cierre
            # del bloque (mañana/tarde) en el que comienza.
            # ------------------------------------------------

            if not _fits_closing(
                appointment_date, appointment_time, end_minutes, business_id, connection=connection
            ):
                connection.execute("ROLLBACK")
                return {"success": False, "appointment_id": None, "reason": "invalid_time"}

            # ------------------------------------------------
            # VERIFICAR SOLAPAMIENTO POR INTERVALO
            #
            # new_start < existing_end AND new_end > existing_start
            # scoped por business_id, solo turnos confirmados.
            # Cubre también el caso de inicio idéntico.
            #
            # Con recurso:
            #   - bloquean este recurso (resource_id = ?)
            #   - bloquean los turnos globales (resource_id IS NULL)
            #   - NO bloquean otros recursos.
            # Sin recurso (bloqueo global):
            #   - bloquea todo el negocio (comportamiento histórico).
            # ------------------------------------------------

            if resource_id_validated is not None:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM appointments
                    WHERE appointment_date = ?
                    AND status = 'confirmed'
                    AND business_id = ?
                    AND (resource_id = ? OR resource_id IS NULL)
                    AND ? < appointment_end
                    AND ? > appointment_time
                    """,
                    (
                        appointment_date,
                        business_id,
                        resource_id_validated,
                        appointment_time,
                        appointment_end,
                    ),
                ).fetchone()
            else:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM appointments
                    WHERE appointment_date = ?
                    AND status = 'confirmed'
                    AND business_id = ?
                    AND ? < appointment_end
                    AND ? > appointment_time
                    """,
                    (appointment_date, business_id, appointment_time, appointment_end),
                ).fetchone()

            if existing:
                connection.execute("ROLLBACK")
                return {"success": False, "appointment_id": None, "reason": "occupied"}

            # ------------------------------------------------
            # CREAR TURNO
            # ------------------------------------------------

            management_token = secrets.token_urlsafe(32)
            management_token_hash = hashlib.sha256(management_token.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO appointments
                (customer_name, phone, customer_email, service, appointment_date,
                 appointment_time, appointment_end, duration, business_id,
                 resource_id, management_token_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_name,
                    normalize_phone(phone),
                    customer_email or None,
                    service,
                    appointment_date,
                    appointment_time,
                    appointment_end,
                    duration,
                    business_id,
                    resource_id_validated,
                    management_token_hash,
                ),
            )

            connection.commit()

            return {
                "success": True,
                "appointment_id": cursor.lastrowid,
                "management_token": management_token,
                "customer_email": customer_email or None,
                "customer_name": customer_name,
                "service": service,
                "appointment_date": appointment_date,
                "appointment_time": appointment_time,
                "appointment_end": appointment_end,
                "duration": duration,
                "business_id": business_id,
                "resource_id": resource_id_validated,
                "reason": "created",
            }

        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            return {"success": False, "appointment_id": None, "reason": "occupied"}
        except Exception:
            connection.execute("ROLLBACK")
            raise

    finally:
        connection.close()


# ============================================================
# OBTENER TODOS LOS TURNOS CONFIRMADOS
# ============================================================


def get_appointments(
    status="confirmed", appointment_date=None, business_id=None, limit=None, offset=None
):
    """
    Devuelve todos los turnos confirmados con soporte de paginación.

    Args:
        status: Estado a filtrar (None para todos)
        appointment_date: Fecha a filtrar (None para todas)
        business_id: ID del negocio (obligatorio)
        limit: Máximo de resultados (None para sin límite)
        offset: Desplazamiento para paginación (None para 0)
    """

    if business_id is None:
        raise ValueError("business_id es obligatorio")
    connection = get_connection()

    try:
        params = [status, status, appointment_date, appointment_date, business_id]
        query = """
            SELECT *
            FROM appointments
                    WHERE (? IS NULL OR status = ?)
                        AND (? IS NULL OR appointment_date = ?)
                        AND business_id = ?
                    ORDER BY appointment_date, appointment_time
                    """
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
            if offset is not None:
                query += " OFFSET ?"
                params.append(offset)

        rows = connection.execute(query, params).fetchall()

        resource_by_id = {}
        if business_id is not None:
            for r in connection.execute(
                "SELECT id, name FROM resources WHERE business_id = ? AND active = 1 ORDER BY name",
                (business_id,),
            ).fetchall():
                resource_by_id[r["id"]] = r["name"]

        result = []
        for row in rows:
            item = dict(row)
            rid = item.get("resource_id")
            if rid is not None and rid in resource_by_id:
                item["resource_name"] = resource_by_id[rid]
            result.append(item)
        return result

    finally:
        connection.close()


def get_appointment_counts(business_id=None):
    """Devuelve métricas agrupadas por estado para el panel administrativo.

    Incluye un conteo separado de turnos "próximos": turnos confirmados cuya
    fecha es hoy o posterior en la zona horaria del negocio.
    """
    if business_id is None:
        raise ValueError("business_id es obligatorio")
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT status, appointment_date, COUNT(*) AS total "
            "FROM appointments WHERE business_id = ? GROUP BY status, appointment_date",
            (business_id,),
        ).fetchall()
        counts = {}
        upcoming = 0
        today = datetime.now(ZoneInfo(get_business_timezone(business_id))).date()
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + row["total"]
            if row["status"] == "confirmed":
                try:
                    apt_date = datetime.strptime(row["appointment_date"], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    apt_date = datetime.min.date()
                if apt_date >= today:
                    upcoming += row["total"]
        return {
            "total": sum(counts.values()),
            "confirmed": counts.get("confirmed", 0),
            "cancelled": counts.get("cancelled", 0),
            "completed": counts.get("completed", 0),
            "no_show": counts.get("no_show", 0),
            "upcoming": upcoming,
        }
    finally:
        connection.close()


# ============================================================
# OBTENER TURNOS DE UN CLIENTE
# ============================================================

# Columnas seguras para exponer públicamente vía /api/turnos / AI.
# NUNCA incluir management_token_hash ni ningún secreto.
PUBLIC_APPOINTMENT_COLUMNS = (
    "id",
    "customer_name",
    "phone",
    "customer_email",
    "service",
    "appointment_date",
    "appointment_time",
    "appointment_end",
    "duration",
    "status",
    "created_at",
    "business_id",
)


def get_customer_appointments(customer_name, phone=None, business_id=None):
    """
    Busca los turnos confirmados de un cliente.

    La búsqueda principal se realiza por nombre.

    Si además se proporciona teléfono,
    se utiliza como filtro adicional.

    Devuelve SOLO columnas públicas (nunca management_token_hash).
    """

    if business_id is None:
        raise ValueError("business_id es obligatorio")
    connection = get_connection()

    try:
        if phone:
            rows = connection.execute(
                f"""
                SELECT {", ".join(PUBLIC_APPOINTMENT_COLUMNS)}
                FROM appointments
                WHERE LOWER(customer_name) = LOWER(?)
                AND phone = ?
                AND status = 'confirmed'
                AND business_id = ?
                ORDER BY appointment_date, appointment_time
                """,
                (customer_name, normalize_phone(phone), business_id),
            ).fetchall()

        else:
            rows = connection.execute(
                f"""
                SELECT {", ".join(PUBLIC_APPOINTMENT_COLUMNS)}
                FROM appointments
                WHERE LOWER(customer_name) = LOWER(?)
                AND status = 'confirmed'
                AND business_id = ?
                ORDER BY appointment_date, appointment_time
                """,
                (customer_name, business_id),
            ).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()


# ============================================================
# CANCELAR TURNO
# ============================================================


def get_appointment_by_token(appointment_id, business_id, management_token):
    """Devuelve el turno de un negocio si el token de gestión es correcto.

    Se usa en la página pública de gestión para ver/cancelar/reprogramar un
    turno usando el enlace seguro firmado con el management_token.
    """
    from database.database import get_appointment_by_token_scoped

    if business_id is None:
        raise ValueError("business_id es obligatorio")
    if not management_token:
        return None
    return get_appointment_by_token_scoped(business_id, appointment_id, management_token)


def cancel_appointment(
    appointment_id, phone, business_id=None, management_token=None, customer_name=None
):
    """
    Cancela un turno confirmado.

    Autorización OBLIGATORIA:
    - `management_token` válido para el turno (enlace seguro).

    Con esto, conocer solo el `appointment_id` (que es incremental y
    enumerable) NO alcanza como único factor para operar sobre turnos ajenos:
    siempre se exige el token de gestión (enlace seguro).

    Usa BEGIN IMMEDIATE para transacción atómica.
    Hace ROLLBACK si algo falla.

    Devuelve:
        True  -> cancelado correctamente
        False -> validación falló o no existe o ya no está confirmado
    """

    if business_id is None:
        raise ValueError("business_id es obligatorio")

    # --------------------------------------------------------
    # VALIDAR FACTORES DE AUTORIZACIÓN
    # --------------------------------------------------------

    if management_token is None or not isinstance(management_token, str) or not management_token:
        return False

    # --------------------------------------------------------
    # VALIDAR APPOINTMENT_ID
    # --------------------------------------------------------

    try:
        appointment_id_int = int(appointment_id)
        if appointment_id_int <= 0:
            return False
    except (ValueError, TypeError):
        return False

    token_hash = hashlib.sha256(management_token.encode()).hexdigest()

    connection = get_connection()

    try:
        # Iniciar transacción atómica
        connection.execute("BEGIN IMMEDIATE")

        try:
            # ------------------------------------------------
            # VERIFICAR QUE EXISTE Y PERTENECE AL SOLICITANTE
            # ------------------------------------------------

            existing = connection.execute(
                """
                SELECT id
                FROM appointments
                WHERE id = ?
                AND status = 'confirmed'
                AND business_id = ?
                AND management_token_hash = ?
                """,
                (appointment_id_int, business_id, token_hash),
            ).fetchone()

            if existing is None:
                connection.execute("ROLLBACK")
                return False

            # ------------------------------------------------
            # CANCELAR TURNO
            # ------------------------------------------------

            cursor = connection.execute(
                """
                UPDATE appointments
                SET status = 'cancelled'
                WHERE id = ?
                AND status = 'confirmed'
                AND business_id = ?
                AND management_token_hash = ?
                """,
                (appointment_id_int, business_id, token_hash),
            )

            connection.commit()

            return cursor.rowcount > 0

        except Exception:
            connection.execute("ROLLBACK")
            raise

    finally:
        connection.close()


# ============================================================
# REPROGRAMAR TURNO
# ============================================================


def reschedule_appointment(
    appointment_id,
    new_date,
    new_time,
    phone,
    business_id=None,
    management_token=None,
    customer_name=None,
):
    """
    Cambia la fecha y hora de un turno confirmado.

    Autorización OBLIGATORIA:
    - `management_token` válido para el turno (enlace seguro).

    El turno debe existir, estar `confirmed` y pertenecer a `business_id`, y
    el hash SHA-256 del token debe coincidir con el almacenado. Conocer solo el
    `appointment_id` (incremental y enumerable) NO alcanza y tampoco se
    autoriza por teléfono + nombre: el token es el único factor de gestión.

    Valida además: datetime, fecha no pasada, no domingo, horario de atención,
    cupo (slot disponible) y cierre.

    Usa BEGIN IMMEDIATE para transacción atómica.
    Hace ROLLBACK si algo falla.
    """

    if business_id is None:
        raise ValueError("business_id es obligatorio")

    # --------------------------------------------------------
    # VALIDAR FACTORES DE AUTORIZACIÓN
    # --------------------------------------------------------

    if management_token is None or not isinstance(management_token, str) or not management_token:
        return {"success": False, "reason": "not_found"}

    # --------------------------------------------------------
    # VALIDAR APPOINTMENT_ID
    # --------------------------------------------------------

    try:
        appointment_id_int = int(appointment_id)
        if appointment_id_int <= 0:
            return {"success": False, "reason": "invalid_appointment_id"}
    except (ValueError, TypeError):
        return {"success": False, "reason": "invalid_appointment_id"}

    token_hash = hashlib.sha256(management_token.encode()).hexdigest()

    connection = get_connection()

    try:
        # Iniciar transacción atómica
        connection.execute("BEGIN IMMEDIATE")

        try:
            # ------------------------------------------------
            # BUSCAR TURNO ORIGINAL
            # ------------------------------------------------

            appointment = connection.execute(
                """
                SELECT *
                FROM appointments
                WHERE id = ?
                AND status = 'confirmed'
                AND business_id = ?
                AND management_token_hash = ?
                """,
                (appointment_id_int, business_id, token_hash),
            ).fetchone()

            if appointment is None:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "not_found"}

            # Conservar la duración histórica del turno (no la del servicio
            # actual). Un cambio posterior de duración del servicio no altera
            # el turno existente.
            historical_duration = appointment["duration"]
            start_minutes = _to_minutes(new_time)
            end_minutes = start_minutes + historical_duration
            new_end = _to_hhmm(end_minutes)

            # ------------------------------------------------
            # VALIDAR NUEVA FECHA
            # ------------------------------------------------

            validation = validate_appointment_date(new_date, business_id, connection=connection)

            if not validation["valid"]:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": validation["reason"]}

            # ------------------------------------------------
            # VALIDAR NUEVO HORARIO
            # ------------------------------------------------

            if new_time not in get_available_slots(
                new_date, business_id, duration=historical_duration, connection=connection
            ):
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "invalid_time"}

            # ------------------------------------------------
            # VALIDAR HORARIO DE CIERRE
            # ------------------------------------------------

            if not _fits_closing(
                new_date, new_time, end_minutes, business_id, connection=connection
            ):
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "invalid_time"}

            # ------------------------------------------------
            # VERIFICAR SOLAPAMIENTO POR INTERVALO
            #
            # Excluimos el propio turno que estamos moviendo.
            #
            # El turno conserva su resource_id histórico: si tenía recurso,
            # sigue bloqueando solo ese recurso (+ bloqueos globales). Si no
            # tenía recurso, sigue bloqueando todo el negocio.
            # ------------------------------------------------

            historical_resource_id = appointment["resource_id"]
            if historical_resource_id is not None:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM appointments
                    WHERE appointment_date = ?
                    AND status = 'confirmed'
                    AND id != ?
                    AND business_id = ?
                    AND (resource_id = ? OR resource_id IS NULL)
                    AND ? < appointment_end
                    AND ? > appointment_time
                    """,
                    (
                        new_date,
                        appointment_id_int,
                        business_id,
                        historical_resource_id,
                        new_time,
                        new_end,
                    ),
                ).fetchone()
            else:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM appointments
                    WHERE appointment_date = ?
                    AND status = 'confirmed'
                    AND id != ?
                    AND business_id = ?
                    AND ? < appointment_end
                    AND ? > appointment_time
                    """,
                    (new_date, appointment_id_int, business_id, new_time, new_end),
                ).fetchone()

            if existing:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "occupied"}

            # ------------------------------------------------
            # ACTUALIZAR TURNO
            # ------------------------------------------------

            connection.execute(
                """
                UPDATE appointments
                SET appointment_date = ?, appointment_time = ?, appointment_end = ?
                WHERE id = ? AND status = 'confirmed' AND business_id = ?
                AND management_token_hash = ?
                """,
                (new_date, new_time, new_end, appointment_id_int, business_id, token_hash),
            )

            connection.commit()

            return {"success": True, "reason": "rescheduled", "resource_id": historical_resource_id}

        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            return {"success": False, "reason": "occupied"}
        except Exception:
            connection.execute("ROLLBACK")
            raise

    finally:
        connection.close()


def reschedule_appointment_admin(
    appointment_id, new_date, new_time, business_id=None, resource_id=None
):
    """
    Cambia la fecha y hora de un turno confirmado desde el panel admin.

    A diferencia de ``reschedule_appointment`` (orientado al cliente, que exige
    teléfono o ``management_token``), esta variante solo exige pertenencia del
    turno al ``business_id`` actual. Mantiene las mismas validaciones de dominio
    (fecha válida, no pasada, no cerrado, horario válido, sin solapamiento) en
    una transacción atómica con ``BEGIN IMMEDIATE``.

    Si se provee ``resource_id``, cambia el recurso asociado del turno. El
    recurso indicado se valida contra el mismo ``business_id`` antes de
    proceder.
    """

    if business_id is None:
        raise ValueError("business_id es obligatorio")

    try:
        appointment_id_int = int(appointment_id)
        if appointment_id_int <= 0:
            return {"success": False, "reason": "invalid_appointment_id"}
    except (ValueError, TypeError):
        return {"success": False, "reason": "invalid_appointment_id"}

    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        try:
            appointment = connection.execute(
                """
                SELECT *
                FROM appointments
                WHERE id = ?
                AND status = 'confirmed'
                AND business_id = ?
                """,
                (appointment_id_int, business_id),
            ).fetchone()

            if appointment is None:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "not_found"}

            historical_duration = appointment["duration"]
            start_minutes = _to_minutes(new_time)
            end_minutes = start_minutes + historical_duration
            new_end = _to_hhmm(end_minutes)

            validation = validate_appointment_date(new_date, business_id, connection=connection)
            if not validation["valid"]:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": validation["reason"]}

            time_valid, time_reason = validate_appointment_time(
                new_date, new_time, business_id, connection=connection
            )
            if not time_valid:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": time_reason}

            if new_time not in get_available_slots(
                new_date, business_id, duration=historical_duration, connection=connection
            ):
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "invalid_time"}

            if not _fits_closing(
                new_date, new_time, end_minutes, business_id, connection=connection
            ):
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "invalid_time"}

            # ------------------------------------------------
            # VERIFICAR SOLAPAMIENTO POR INTERVALO
            #
            # Excluimos el propio turno que estamos moviendo.
            #
            # El turno conserva su resource_id histórico: si tenía recurso,
            # sigue bloqueando solo ese recurso (+ bloqueos globales). Si no
            # tenía recurso, sigue bloqueando todo el negocio.
            # ------------------------------------------------

            historical_resource_id = appointment["resource_id"]
            if historical_resource_id is not None:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM appointments
                    WHERE appointment_date = ?
                    AND status = 'confirmed'
                    AND id != ?
                    AND business_id = ?
                    AND (resource_id = ? OR resource_id IS NULL)
                    AND ? < appointment_end
                    AND ? > appointment_time
                    """,
                    (
                        new_date,
                        appointment_id_int,
                        business_id,
                        historical_resource_id,
                        new_time,
                        new_end,
                    ),
                ).fetchone()
            else:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM appointments
                    WHERE appointment_date = ?
                    AND status = 'confirmed'
                    AND id != ?
                    AND business_id = ?
                    AND ? < appointment_end
                    AND ? > appointment_time
                    """,
                    (new_date, appointment_id_int, business_id, new_time, new_end),
                ).fetchone()

            if existing:
                connection.execute("ROLLBACK")
                return {"success": False, "reason": "occupied"}

            connection.execute(
                """
                UPDATE appointments
                SET appointment_date = ?, appointment_time = ?, appointment_end = ?
                WHERE id = ? AND status = 'confirmed' AND business_id = ?
                """,
                (new_date, new_time, new_end, appointment_id_int, business_id),
            )

            connection.commit()
            return {"success": True, "reason": "rescheduled", "resource_id": historical_resource_id}

        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            return {"success": False, "reason": "occupied"}
        except Exception:
            connection.execute("ROLLBACK")
            raise

    finally:
        connection.close()
