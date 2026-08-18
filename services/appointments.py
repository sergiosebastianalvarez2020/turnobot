import sqlite3
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from database.database import (
    get_active_services,
    get_business_settings,
    get_connection,
    get_weekly_schedule,
)


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
    
    # Remover espacios en blanco
    phone_clean = phone.strip()
    
    # Verificar que solo contenga dígitos
    if not phone_clean.isdigit():
        return False
    
    # Verificar que tenga al menos 7 dígitos
    if len(phone_clean) < 7:
        return False
    
    return True


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


# ============================================================
# HORARIOS DISPONIBLES
# ============================================================

def get_available_slots(date):
    """Construye los horarios a partir de la configuración del negocio."""
    appointment_date = datetime.strptime(date, "%Y-%m-%d").date()
    schedule = get_weekly_schedule(appointment_date.weekday())
    settings = get_business_settings()

    if not schedule or not schedule["is_open"]:
        return []

    slot_duration = settings["slot_duration"] if settings else 60
    break_between_slots = settings["break_between_slots"] if settings else 0
    services = get_active_services()
    longest_service = max(
        (service["duration"] for service in services),
        default=slot_duration,
    )
    slot_interval = max(slot_duration, longest_service) + break_between_slots
    slots = []
    for start, end in (
        (schedule["morning_start"], schedule["morning_end"]),
        (schedule["afternoon_start"], schedule["afternoon_end"]),
    ):
        if not start or not end:
            continue
        current = datetime.strptime(start, "%H:%M")
        closing = datetime.strptime(end, "%H:%M")
        while current < closing:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=slot_interval)
    return slots


# ============================================================
# VALIDAR FECHA
# ============================================================

def validate_appointment_date(date):
    """
    Valida que la fecha del turno:

    - tenga formato YYYY-MM-DD
    - no sea anterior a hoy
    - no sea domingo

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

        appointment_date = datetime.strptime(
            date,
            "%Y-%m-%d"
        ).date()

    except (ValueError, TypeError):

        return {
            "valid": False,
            "reason": "invalid_date",
        }


    today = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()


    # --------------------------------------------------------
    # FECHA PASADA
    # --------------------------------------------------------

    if appointment_date < today:

        return {
            "valid": False,
            "reason": "past_date",
        }


    schedule = get_weekly_schedule(appointment_date.weekday())

    if not schedule or not schedule["is_open"]:

        return {
            "valid": False,
            "reason": "closed_day",
        }


    return {
        "valid": True,
        "reason": None,
    }


# ============================================================
# CONSULTAR DISPONIBILIDAD
# ============================================================

def get_available_times(date):
    """
    Devuelve los horarios que todavía están libres
    para una determinada fecha.

    Si la fecha no es válida, es pasada o es domingo,
    devuelve una lista vacía.
    """

    validation = validate_appointment_date(date)


    if not validation["valid"]:

        return []


    connection = get_connection()

    try:

        rows = connection.execute(
            """
            SELECT appointment_time
            FROM appointments
            WHERE appointment_date = ?
            AND status = 'confirmed'
            """,
            (date,),
        ).fetchall()


        occupied = {
            row["appointment_time"]
            for row in rows
        }


        return [
            time
            for time in get_available_slots(date)
            if time not in occupied
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
):
    """
    Crea un turno.

    Valida:

    - nombre válido (al menos 2 caracteres)
    - teléfono válido (solo dígitos, al menos 7)
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
            "reason": "created"
        }

    Error:

        {
            "success": False,
            "appointment_id": None,
            "reason": "invalid_name|invalid_phone|occupied|..."
        }
    """

    # --------------------------------------------------------
    # VALIDAR NOMBRE
    # --------------------------------------------------------

    if not validate_customer_name(customer_name):
        return {
            "success": False,
            "appointment_id": None,
            "reason": "invalid_name",
        }

    # --------------------------------------------------------
    # VALIDAR TELÉFONO
    # --------------------------------------------------------

    if not validate_phone(phone):
        return {
            "success": False,
            "appointment_id": None,
            "reason": "invalid_phone",
        }

    # --------------------------------------------------------
    # VALIDAR FECHA
    # --------------------------------------------------------

    validation = validate_appointment_date(
        appointment_date
    )

    if not validation["valid"]:
        return {
            "success": False,
            "appointment_id": None,
            "reason": validation["reason"],
        }

    # --------------------------------------------------------
    # VALIDAR HORARIO
    # --------------------------------------------------------

    if appointment_time not in get_available_slots(appointment_date):
        return {
            "success": False,
            "appointment_id": None,
            "reason": "invalid_time",
        }

    connection = get_connection()

    try:

        # Iniciar transacción atómica
        connection.execute("BEGIN IMMEDIATE")

        try:

            # ------------------------------------------------
            # VERIFICAR HORARIO OCUPADO
            # ------------------------------------------------

            existing = connection.execute(
                """
                SELECT id
                FROM appointments
                WHERE appointment_date = ?
                AND appointment_time = ?
                AND status = 'confirmed'
                """,
                (
                    appointment_date,
                    appointment_time,
                ),
            ).fetchone()

            if existing:
                connection.execute("ROLLBACK")
                return {
                    "success": False,
                    "appointment_id": None,
                    "reason": "occupied",
                }

            # ------------------------------------------------
            # CREAR TURNO
            # ------------------------------------------------

            cursor = connection.execute(
                """
                INSERT INTO appointments
                (customer_name, phone, service, appointment_date, appointment_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (customer_name, phone, service, appointment_date, appointment_time),
            )

            connection.commit()

            return {
                "success": True,
                "appointment_id": cursor.lastrowid,
                "reason": "created",
            }

        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            return {"success": False, "appointment_id": None, "reason": "occupied"}
        except Exception as e:
            connection.execute("ROLLBACK")
            raise

    finally:
        connection.close()


# ============================================================
# OBTENER TODOS LOS TURNOS CONFIRMADOS
# ============================================================

def get_appointments():
    """
    Devuelve todos los turnos confirmados.
    """

    connection = get_connection()

    try:

        rows = connection.execute(
            """
            SELECT *
            FROM appointments
            WHERE status = 'confirmed'
            ORDER BY appointment_date, appointment_time
            """
        ).fetchall()


        return [
            dict(row)
            for row in rows
        ]


    finally:

        connection.close()


# ============================================================
# OBTENER TURNOS DE UN CLIENTE
# ============================================================

def get_customer_appointments(
    customer_name,
    phone=None,
):
    """
    Busca los turnos confirmados de un cliente.

    La búsqueda principal se realiza por nombre.

    Si además se proporciona teléfono,
    se utiliza como filtro adicional.
    """

    connection = get_connection()

    try:

        if phone:

            rows = connection.execute(
                """
                SELECT *
                FROM appointments
                WHERE LOWER(customer_name) = LOWER(?)
                AND phone = ?
                AND status = 'confirmed'
                ORDER BY appointment_date, appointment_time
                """,
                (
                    customer_name,
                    phone,
                ),
            ).fetchall()

        else:

            rows = connection.execute(
                """
                SELECT *
                FROM appointments
                WHERE LOWER(customer_name) = LOWER(?)
                AND status = 'confirmed'
                ORDER BY appointment_date, appointment_time
                """,
                (
                    customer_name,
                ),
            ).fetchall()


        return [
            dict(row)
            for row in rows
        ]


    finally:

        connection.close()


# ============================================================
# CANCELAR TURNO
# ============================================================

def cancel_appointment(appointment_id, phone):
    """
    Cancela un turno confirmado.

    Valida:
    - teléfono válido (solo dígitos, al menos 7)
    - appointment_id sea un entero positivo
    - que el turno existe y pertenece al teléfono
    - que el turno está confirmado

    Usa BEGIN IMMEDIATE para transacción atómica.
    Hace ROLLBACK si algo falla.

    Devuelve:
        True  -> cancelado correctamente
        False -> validación falló o no existe o ya no está confirmado
    """

    # --------------------------------------------------------
    # VALIDAR TELÉFONO
    # --------------------------------------------------------

    if not validate_phone(phone):
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

    connection = get_connection()

    try:

        # Iniciar transacción atómica
        connection.execute("BEGIN IMMEDIATE")

        try:

            # ------------------------------------------------
            # VERIFICAR QUE EXISTE Y PERTENECE AL TELÉFONO
            # ------------------------------------------------

            existing = connection.execute(
                """
                SELECT id
                FROM appointments
                WHERE id = ?
                AND phone = ?
                AND status = 'confirmed'
                """,
                (appointment_id_int, phone),
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
                AND phone = ?
                AND status = 'confirmed'
                """,
                (appointment_id_int, phone),
            )

            connection.commit()

            return cursor.rowcount > 0

        except Exception as e:
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
):
    """
    Cambia la fecha y hora de un turno confirmado.

    Valida:
    - teléfono válido (solo dígitos, al menos 7)
    - appointment_id sea un entero positivo
    - que el turno original exista
    - que la nueva fecha sea válida
    - que no sea una fecha pasada
    - que no sea domingo
    - que el horario sea válido
    - que el nuevo horario esté libre

    Usa BEGIN IMMEDIATE para transacción atómica.
    Hace ROLLBACK si algo falla.
    """

    # --------------------------------------------------------
    # VALIDAR TELÉFONO
    # --------------------------------------------------------

    if not validate_phone(phone):
        return {
            "success": False,
            "reason": "invalid_phone",
        }

    # --------------------------------------------------------
    # VALIDAR APPOINTMENT_ID
    # --------------------------------------------------------

    try:
        appointment_id_int = int(appointment_id)
        if appointment_id_int <= 0:
            return {
                "success": False,
                "reason": "invalid_appointment_id",
            }
    except (ValueError, TypeError):
        return {
            "success": False,
            "reason": "invalid_appointment_id",
        }

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
                AND phone = ?
                AND status = 'confirmed'
                """,
                (appointment_id_int, phone),
            ).fetchone()

            if appointment is None:
                connection.execute("ROLLBACK")
                return {
                    "success": False,
                    "reason": "not_found",
                }

            # ------------------------------------------------
            # VALIDAR NUEVA FECHA
            # ------------------------------------------------

            validation = validate_appointment_date(
                new_date
            )

            if not validation["valid"]:
                connection.execute("ROLLBACK")
                return {
                    "success": False,
                    "reason": validation["reason"],
                }

            # ------------------------------------------------
            # VALIDAR NUEVO HORARIO
            # ------------------------------------------------

            if new_time not in get_available_slots(new_date):
                connection.execute("ROLLBACK")
                return {
                    "success": False,
                    "reason": "invalid_time",
                }

            # ------------------------------------------------
            # VERIFICAR DISPONIBILIDAD
            #
            # Excluimos el propio turno que estamos moviendo.
            # ------------------------------------------------

            existing = connection.execute(
                """
                SELECT id
                FROM appointments
                WHERE appointment_date = ?
                AND appointment_time = ?
                AND status = 'confirmed'
                AND id != ?
                """,
                (
                    new_date,
                    new_time,
                    appointment_id_int,
                ),
            ).fetchone()

            if existing:
                connection.execute("ROLLBACK")
                return {
                    "success": False,
                    "reason": "occupied",
                }

            # ------------------------------------------------
            # ACTUALIZAR TURNO
            # ------------------------------------------------

            connection.execute(
                """
                UPDATE appointments
                SET appointment_date = ?, appointment_time = ?
                WHERE id = ? AND phone = ? AND status = 'confirmed'
                """,
                (new_date, new_time, appointment_id_int, phone),
            )

            connection.commit()

            return {
                "success": True,
                "reason": "rescheduled",
            }

        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            return {"success": False, "reason": "occupied"}
        except Exception as e:
            connection.execute("ROLLBACK")
            raise

    finally:
        connection.close()
