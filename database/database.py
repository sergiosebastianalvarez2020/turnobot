import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_PATH = BASE_DIR / "database" / "appointments.db"


def get_connection():

    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():

    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    connection = get_connection()


    # ========================================================
    # TURNOS
    # ========================================================

    connection.execute("""
        CREATE TABLE IF NOT EXISTS appointments (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            customer_name TEXT NOT NULL,

            phone TEXT,

            service TEXT NOT NULL,

            appointment_date TEXT NOT NULL,

            appointment_time TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'confirmed',

            created_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP

        )
    """)

    # Evita que dos solicitudes simultáneas confirmen el mismo horario.
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_confirmed_appointment_slot
        ON appointments (appointment_date, appointment_time)
        WHERE status = 'confirmed'
    """)


    # ========================================================
    # CONFIGURACIÓN DEL NEGOCIO
    # ========================================================

    connection.execute("""
        CREATE TABLE IF NOT EXISTS business_settings (

            id INTEGER PRIMARY KEY CHECK (id = 1),

            business_name TEXT
                DEFAULT 'Mi negocio',

            slot_duration INTEGER
                NOT NULL DEFAULT 60,

            break_between_slots INTEGER
                NOT NULL DEFAULT 0

        )
    """)


    # ========================================================
    # HORARIOS SEMANALES
    # ========================================================

    connection.execute("""
        CREATE TABLE IF NOT EXISTS weekly_schedules (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            day_of_week INTEGER NOT NULL,

            is_open INTEGER NOT NULL DEFAULT 0,

            morning_start TEXT,

            morning_end TEXT,

            afternoon_start TEXT,

            afternoon_end TEXT,

            UNIQUE(day_of_week)

        )
    """)


    # ========================================================
    # SERVICIOS
    # ========================================================

    connection.execute("""
        CREATE TABLE IF NOT EXISTS services (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL,

            price REAL NOT NULL DEFAULT 0,

            duration INTEGER NOT NULL DEFAULT 60,

            active INTEGER NOT NULL DEFAULT 1

        )
    """)


    # ========================================================
    # CONFIGURACIÓN INICIAL
    # ========================================================

    connection.execute("""
        INSERT OR IGNORE INTO business_settings
        (
            id,
            business_name,
            slot_duration,
            break_between_slots
        )
        VALUES
        (
            1,
            'El Corte',
            60,
            0
        )
    """)


    # ========================================================
    # DÍAS DE LA SEMANA
    #
    # 0 = lunes
    # 1 = martes
    # 2 = miércoles
    # 3 = jueves
    # 4 = viernes
    # 5 = sábado
    # 6 = domingo
    # ========================================================

    weekly_days = [

        (
            0,
            1,
            "09:00",
            "13:00",
            "15:00",
            "20:00"
        ),

        (
            1,
            1,
            "09:00",
            "13:00",
            "15:00",
            "20:00"
        ),

        (
            2,
            1,
            "09:00",
            "13:00",
            "15:00",
            "20:00"
        ),

        (
            3,
            1,
            "09:00",
            "13:00",
            "15:00",
            "20:00"
        ),

        (
            4,
            1,
            "09:00",
            "13:00",
            "15:00",
            "20:00"
        ),

        (
            5,
            1,
            "09:00",
            "13:00",
            None,
            None
        ),

        (
            6,
            0,
            None,
            None,
            None,
            None
        )
    ]


    connection.executemany("""
        INSERT OR IGNORE INTO weekly_schedules
        (
            day_of_week,
            is_open,
            morning_start,
            morning_end,
            afternoon_start,
            afternoon_end
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, weekly_days)


    # ========================================================
    # SERVICIOS INICIALES
    # ========================================================

    connection.execute("""
        INSERT OR IGNORE INTO services
        (
            id,
            name,
            price,
            duration,
            active
        )
        VALUES
        (
            1,
            'Corte',
            10000,
            30,
            1
        )
    """)


    connection.execute("""
        INSERT OR IGNORE INTO services
        (
            id,
            name,
            price,
            duration,
            active
        )
        VALUES
        (
            2,
            'Corte + barba',
            15000,
            50,
            1
        )
    """)


    connection.execute("""
        INSERT OR IGNORE INTO services
        (
            id,
            name,
            price,
            duration,
            active
        )
        VALUES
        (
            3,
            'Barba',
            7000,
            20,
            1
        )
    """)

    try:
        connection.commit()
    finally:
        connection.close()


def get_active_services():
    """Devuelve los servicios que el negocio tiene habilitados."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT name, price, duration
            FROM services
            WHERE active = 1
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()


def get_business_settings():
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT business_name, slot_duration, break_between_slots
            FROM business_settings
            WHERE id = 1
            """
        ).fetchone()
    finally:
        connection.close()


def get_weekly_schedule(day_of_week):
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT * FROM weekly_schedules WHERE day_of_week = ?",
            (day_of_week,),
        ).fetchone()
    finally:
        connection.close()
