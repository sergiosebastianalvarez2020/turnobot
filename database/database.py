import os
import sqlite3
import re
import hashlib
from pathlib import Path
from werkzeug.security import generate_password_hash


BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_PATH = BASE_DIR / "database" / "appointments.db"

MIGRATIONS_DIR = BASE_DIR / "migrations"


def get_connection():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        DATABASE_PATH, timeout=10, check_same_thread=False
    )
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# SISTEMA DE MIGRACIONES
# ============================================================

def _get_current_version(connection):
    try:
        row = connection.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_version(connection, version):
    connection.execute(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
        (version,),
    )


def _migration_version(name):
    return int(name.split("_")[0])


def apply_migrations():
    """
    Aplica migraciones pendientes en orden.

    Lee archivos .sql de migrations/ ordenados por nombre.
    Cada archivo se ejecuta con executescript (auto-commit).
    Si un archivo falla, se detiene y se muestra el error.
    """
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)

    connection = get_connection()

    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.commit()

        current_version = _get_current_version(connection)

        all_entries = os.listdir(str(MIGRATIONS_DIR))
        migration_files = sorted(
            MIGRATIONS_DIR / name
            for name in all_entries
            if name.endswith(".sql") and name.split("_")[0].isdigit()
        )

        pending = [
            f for f in migration_files
            if _migration_version(f.stem) > current_version
        ]

        if not pending:
            return

        for migration_file in pending:
            version = _migration_version(migration_file.stem)
            sql = migration_file.read_text(encoding="utf-8")

            print(f"  Aplicando migración {migration_file.name}...")

            try:
                connection.executescript(sql)
                _set_version(connection, version)
                connection.commit()
                print(f"  Migración {migration_file.name} aplicada.")
            except Exception as e:
                connection.rollback()
                print(f"  ERROR en migración {migration_file.name}: {e}")
                raise

    finally:
        connection.close()


def init_database():
    apply_migrations()


def create_business_with_owner(name, email, password):
    """Provisiona un negocio completo en una única transacción.

    Esta operación está destinada a un comando/controlador de plataforma
    confiable; no acepta IDs ni roles del cliente.
    """
    name = name.strip() if isinstance(name, str) else ""
    email = email.strip().lower() if isinstance(email, str) else ""
    password = password if isinstance(password, str) else ""
    if not 2 <= len(name) <= 120:
        raise ValueError("nombre de negocio inválido")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("email inválido")
    if len(password) < 12:
        raise ValueError("la contraseña debe tener al menos 12 caracteres")

    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-") or "negocio"
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        slug = base_slug
        suffix = 2
        while connection.execute("SELECT 1 FROM businesses WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        business_cursor = connection.execute(
            "INSERT INTO businesses (name, slug) VALUES (?, ?)", (name, slug)
        )
        business_id = business_cursor.lastrowid
        user_cursor = connection.execute(
            "INSERT INTO users (email, password_hash, active) VALUES (?, ?, 1)",
            (email, generate_password_hash(password)),
        )
        user_id = user_cursor.lastrowid
        owner_role = connection.execute(
            "SELECT id FROM roles WHERE name = 'owner'"
        ).fetchone()
        connection.execute(
            "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
            (user_id, business_id, owner_role["id"]),
        )
        connection.execute(
            """INSERT INTO business_settings
               (business_name, slot_duration, break_between_slots, business_type,
                business_initials, business_description, timezone, business_id)
               VALUES (?, 60, 0, 'Negocio', ?, '', 'America/Argentina/Buenos_Aires', ?)""",
            (name, "".join(word[0] for word in name.split())[:3].upper(), business_id),
        )
        for day in range(6):
            connection.execute(
                """INSERT INTO weekly_schedules
                   (day_of_week, is_open, morning_start, morning_end,
                    afternoon_start, afternoon_end, business_id)
                   VALUES (?, 1, '09:00', '13:00', '15:00', '20:00', ?)""",
                (day, business_id),
            )
        connection.execute(
            "INSERT INTO weekly_schedules (day_of_week, is_open, business_id) VALUES (6, 0, ?)",
            (business_id,),
        )
        connection.commit()
        return {"business_id": business_id, "slug": slug, "user_id": user_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ============================================================
# CONSULTAS
# ============================================================

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


def get_all_services():
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT id, name, price, duration, active FROM services ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def create_service(name, price, duration, active=True):
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO services (name, price, duration, active) VALUES (?, ?, ?, ?)",
            (name, price, duration, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_service(service_id, name, price, duration, active):
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE services
            SET name = ?, price = ?, duration = ?, active = ?
            WHERE id = ?
            """,
            (name, price, duration, 1 if active else 0, service_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_business_settings():
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT business_name, business_type, business_initials,
                   business_description, timezone,
                   slot_duration, break_between_slots
            FROM business_settings
            WHERE id = 1
            """
        ).fetchone()
    finally:
        connection.close()


def update_business_settings(
    business_name,
    business_type,
    business_initials,
    business_description,
    timezone,
):
    connection = get_connection()
    try:
        connection.execute(
            """
            UPDATE business_settings
            SET business_name = ?,
                business_type = ?,
                business_initials = ?,
                business_description = ?,
                timezone = ?
            WHERE id = 1
            """,
            (
                business_name,
                business_type,
                business_initials,
                business_description,
                timezone,
            ),
        )
        connection.commit()
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


def update_appointment_status(appointment_id, status):
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return False
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ?",
            (status, appointment_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# SERVICIOS — CAPA MULTI-NEGOCIO (SCOPED)
# ============================================================

def get_active_services_scoped(business_id):
    """Devuelve los servicios activos de un negocio específico."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT name, price, duration
            FROM services
            WHERE active = 1 AND business_id = ?
            ORDER BY id
            """,
            (business_id,),
        ).fetchall()
    finally:
        connection.close()


def get_all_services_scoped(business_id):
    """Devuelve todos los servicios de un negocio específico."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, name, price, duration, active
            FROM services
            WHERE business_id = ?
            ORDER BY id
            """,
            (business_id,),
        ).fetchall()
    finally:
        connection.close()


def create_service_scoped(business_id, name, price, duration, active=True):
    """Crea un servicio asociado explícitamente a un negocio."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO services (business_id, name, price, duration, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (business_id, name, price, duration, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_service_scoped(service_id, business_id, name, price, duration, active):
    """Modifica un servicio solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE services
            SET name = ?, price = ?, duration = ?, active = ?
            WHERE id = ? AND business_id = ?
            """,
            (name, price, duration, 1 if active else 0, service_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# CONFIGURACIÓN / HORARIOS / TURNOS — CAPA MULTI-NEGOCIO (SCOPED)
# ============================================================

def list_all_businesses_scoped():
    """Lista negocios con su zona horaria y flag de notificaciones.

    Usado por el runner de recordatorios para resolver "mañana" en la zona
    horaria de cada negocio sin mezclar tenants.
    """
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT b.id, b.slug, b.name,
                   bs.timezone, bs.notifications_enabled
            FROM businesses b
            LEFT JOIN business_settings bs ON bs.business_id = b.id
            ORDER BY b.id
            """
        ).fetchall()
    finally:
        connection.close()


def get_business_settings_scoped(business_id):
    """Devuelve la configuración de un negocio específico."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT business_name, business_type, business_initials,
                   business_description, timezone,
                   slot_duration, break_between_slots,
                   notifications_enabled
            FROM business_settings
            WHERE business_id = ?
            """,
            (business_id,),
        ).fetchone()
    finally:
        connection.close()


def update_business_settings_scoped(
    business_id,
    business_name,
    business_type,
    business_initials,
    business_description,
    timezone,
    notifications_enabled=None,
):
    """Actualiza la configuración de un negocio específico."""
    connection = get_connection()
    try:
        if notifications_enabled is None:
            connection.execute(
                """
                UPDATE business_settings
                SET business_name = ?,
                    business_type = ?,
                    business_initials = ?,
                    business_description = ?,
                    timezone = ?
                WHERE business_id = ?
                """,
                (
                    business_name,
                    business_type,
                    business_initials,
                    business_description,
                    timezone,
                    business_id,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE business_settings
                SET business_name = ?,
                    business_type = ?,
                    business_initials = ?,
                    business_description = ?,
                    timezone = ?,
                    notifications_enabled = ?
                WHERE business_id = ?
                """,
                (
                    business_name,
                    business_type,
                    business_initials,
                    business_description,
                    timezone,
                    1 if notifications_enabled else 0,
                    business_id,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def set_notifications_enabled_scoped(business_id, enabled):
    """Habilita/deshabilita las notificaciones de un negocio."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE business_settings SET notifications_enabled = ? WHERE business_id = ?",
            (1 if enabled else 0, business_id),
        )
        connection.commit()
    finally:
        connection.close()


def add_notification_log_scoped(appointment_id, business_id, type_, channel, destination):
    """Registra una notificación enviada (idempotente por appointment/type/channel).

    Devuelve True si se insertó por primera vez, False si ya existía o si
    falló la integridad.
    """
    connection = get_connection()
    try:
        try:
            connection.execute(
                """
                INSERT INTO notification_log
                (appointment_id, business_id, type, channel, destination)
                VALUES (?, ?, ?, ?, ?)
                """,
                (appointment_id, business_id, type_, channel, destination),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
    finally:
        connection.close()


def notification_sent_scoped(business_id, appointment_id, type_, channel):
    """Devuelve True si ya se envió una notificación del tipo/canal indicado
    para ese turno dentro del negocio (aislamiento estricto por tenant)."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT 1 FROM notification_log
            WHERE business_id = ? AND appointment_id = ? AND type = ? AND channel = ?
            """,
            (business_id, appointment_id, type_, channel),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def list_reminder_candidates_scoped(remind_date):
    """Turnos confirmados para recordar (misma fecha de turno en todas las
    zonas horarias se aproxima por fecha). Independiente de tenant para que el
    runner resuelva por negocio; el despacho usa get_business_settings_scoped.

    `remind_date` es la fecha del turno (YYYY-MM-DD).
    """
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, business_id, customer_name, customer_email, service,
                   appointment_date, appointment_time, appointment_end,
                   management_token_hash
            FROM appointments
            WHERE status = 'confirmed'
            AND appointment_date = ?
            AND customer_email IS NOT NULL
            AND customer_email != ''
            ORDER BY business_id, appointment_time
            """,
            (remind_date,),
        ).fetchall()
    finally:
        connection.close()


def get_appointment_by_token_scoped(business_id, appointment_id, management_token):
    """Devuelve un turno del negocio si el token de gestión coincide.

    Compara el SHA-256 del token provisto con el hash almacenado (nunca se
    guarda el token en claro). Devuelve fila o None.
    """
    if not management_token:
        return None
    token_hash = hashlib.sha256(management_token.encode("utf-8")).hexdigest()
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT id, customer_name, phone, customer_email, service,
                   appointment_date, appointment_time, appointment_end,
                   duration, status, business_id
            FROM appointments
            WHERE id = ? AND business_id = ? AND management_token_hash = ?
            """,
            (appointment_id, business_id, token_hash),
        ).fetchone()
    finally:
        connection.close()


def get_weekly_schedule_scoped(day_of_week, business_id):
    """Devuelve el horario semanal de un negocio específico para un día."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT *
            FROM weekly_schedules
            WHERE day_of_week = ? AND business_id = ?
            """,
            (day_of_week, business_id),
        ).fetchone()
    finally:
        connection.close()


def update_appointment_status_scoped(appointment_id, status, business_id):
    """Actualiza el estado de un turno solo si pertenece al negocio indicado."""
    if status not in {"confirmed", "cancelled", "completed", "no_show"}:
        return False
    connection = get_connection()
    try:
        cursor = connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ? AND business_id = ?",
            (status, appointment_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# AUTENTICACIÓN / AUTORIZACIÓN MULTINEGOCIO
#
# El tenant SIEMPRE se deriva del slug de la URL (resolve_business) y la
# autorización se comprueba contra business_users. Nunca se confía en
# business_id/role provenientes del cliente.
# ============================================================

def get_user_by_email_scoped(email):
    """Devuelve un usuario por email (con su id y estado)."""
    if not email:
        return None
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT id, email, password_hash, active FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        connection.close()


def get_user_by_id_scoped(user_id):
    """Devuelve un usuario por id."""
    connection = get_connection()
    try:
        return connection.execute(
            "SELECT id, email, password_hash, active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    finally:
        connection.close()


def set_user_password_scoped(user_id, password_hash):
    """Actualiza el hash de contraseña de un usuario."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        connection.commit()
    finally:
        connection.close()


def create_user_scoped(email, password_hash, active=True):
    """Crea un usuario. Devuelve el id creado o None si el email existe."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO users (email, password_hash, active) VALUES (?, ?, ?)",
            (email, password_hash, 1 if active else 0),
        )
        connection.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        connection.rollback()
        return None
    finally:
        connection.close()


def get_role_id_scoped(role_name):
    """Devuelve el id del rol por nombre."""
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT id FROM roles WHERE name = ?", (role_name,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        connection.close()


def get_membership_scoped(user_id, business_id):
    """Devuelve la membresía (user+business+rol) de un usuario en un negocio."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT bu.user_id, bu.business_id, r.name AS role_name
            FROM business_users bu
            JOIN roles r ON r.id = bu.role_id
            WHERE bu.user_id = ? AND bu.business_id = ?
            """,
            (user_id, business_id),
        ).fetchone()
    finally:
        connection.close()


def create_membership_scoped(user_id, business_id, role_name):
    """Asocia un usuario a un negocio con un rol. Devuelve True/False."""
    role_id = get_role_id_scoped(role_name)
    if role_id is None:
        return False
    connection = get_connection()
    try:
        try:
            connection.execute(
                """
                INSERT INTO business_users (user_id, business_id, role_id)
                VALUES (?, ?, ?)
                """,
                (user_id, business_id, role_id),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            connection.rollback()
            return False
    finally:
        connection.close()


def list_members_scoped(business_id):
    """Devuelve los miembros (usuario+rol) de un negocio específico."""
    connection = get_connection()
    try:
        return connection.execute(
            """
            SELECT bu.user_id, bu.business_id, bu.role_id, r.name AS role_name,
                   u.email, u.active
            FROM business_users bu
            JOIN roles r ON r.id = bu.role_id
            JOIN users u ON u.id = bu.user_id
            WHERE bu.business_id = ?
            ORDER BY u.email
            """,
            (business_id,),
        ).fetchall()
    finally:
        connection.close()


def change_membership_role_scoped(user_id, business_id, role_id):
    """Cambia el rol de una membresía solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE business_users
            SET role_id = ?
            WHERE user_id = ? AND business_id = ?
            """,
            (role_id, user_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        connection.rollback()
        return False
    finally:
        connection.close()


def revoke_membership_scoped(user_id, business_id):
    """Elimina una membresía solo si pertenece al negocio indicado."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            DELETE FROM business_users
            WHERE user_id = ? AND business_id = ?
            """,
            (user_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def count_owners_scoped(business_id):
    """Cuenta los owners (rol 'owner') de un negocio específico."""
    owner_role_id = get_role_id_scoped("owner")
    if owner_role_id is None:
        return 0
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS n
            FROM business_users
            WHERE business_id = ? AND role_id = ?
            """,
            (business_id, owner_role_id),
        ).fetchone()
        return row["n"] if row else 0
    finally:
        connection.close()


def create_session_scoped(user_id, token_hash, expires_at):
    """Crea una sesión persistente. Devuelve el id de sesión."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO sessions (user_id, token_hash, expires_at, revoked)
            VALUES (?, ?, ?, 0)
            """,
            (user_id, token_hash, expires_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def revoke_all_sessions_scoped(user_id):
    """Revoca todas las sesiones activas de un usuario (logout completo)."""
    connection = get_connection()
    try:
        connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0",
            (user_id,),
        )
        connection.commit()
    finally:
        connection.close()


def is_session_valid_scoped(user_id, token_hash, now_iso):
    """Devuelve True si existe una sesión activa, no revocada y no expirada."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id FROM sessions
            WHERE user_id = ?
            AND token_hash = ?
            AND revoked = 0
            AND expires_at > ?
            """,
            (user_id, token_hash, now_iso),
        ).fetchone()
        return row is not None
    finally:
        connection.close()
