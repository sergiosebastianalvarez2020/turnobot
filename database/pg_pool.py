"""Módulo de infraestructura y ciclo de vida del Pool de Conexiones PostgreSQL (Fase 4C - 4F).

Proporciona abstracción mínima para la inicialización, acceso y cierre
del pool de conexiones psycopg_pool.ConnectionPool, así como proxies de conexión,
cursor y filas (PgRowProxy, PgCursorProxy, PgConnectionProxy) para adaptar la sintaxis
y acceso de datos de SQLite a PostgreSQL sin alterar la funcionalidad sobre SQLite.
"""

from __future__ import annotations

import atexit
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import psycopg
import psycopg_pool
from psycopg import errors as _psycopg_errors

try:
    from psycopg.pq import TransactionStatus as _PgTransactionStatus
except Exception:  # pragma: no cover - entornos sin psycopg.pq
    _PgTransactionStatus = None  # type: ignore

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger("turnobot.db.pg_pool")

_global_pool: psycopg_pool.ConnectionPool | None = None


# ============================================================
# CONFIGURACIÓN DE SESIÓN POSTGRESQL (hardening pre-producción)
# ============================================================
#
# TurnoBot persiste las marcas de tiempo como TIMESTAMPTZ. PostgreSQL devuelve
# TIMESTAMPTZ usando el timezone de la SESIÓN, no el del servidor: si la sesión
# hereda el timezone del servidor (o el del cliente vía libpq), las expiraciones
# de sesión, invitaciones, notificaciones y la detección de turnos vencidos
# dependen de una configuración externa no controlada por la aplicación. Por eso
# la sesión se fija explícitamente a UTC en CADA conexión nueva del pool, sin
# depender de la configuración del servidor ni del sistema operativo.

PG_SESSION_TIMEZONE = "UTC"

# `lock_timeout` acota la espera por locks (filas y `pg_advisory_xact_lock` de
# `acquire_business_write_lock`). Valor 5s justificado con la configuración ya
# existente del pool:
#   - Es menor que el timeout de checkout del pool (10s) y que el
#     `PRAGMA busy_timeout` de SQLite (10s): un lock nunca retiene una conexión
#     del pool más tiempo del que SQLite ya tolerate por escritura.
#   - Es holgado para el tráfico normal: las transacciones de TurnoBot son
#     escrituras cortas y el advisory lock solo serializa la misma
#     (business_id, lock_key), así que la espera real es de milisegundos.
#   - Ante contención patológica PostgreSQL lanza LockNotAvailable, subclase de
#     psycopg.OperationalError, que `translate_pg_error` mapea a
#     sqlite3.OperationalError: las rutas de "recurso ocupado" ya existentes
#     responden con el mismo código HTTP que en SQLite en vez de colgarse.
PG_LOCK_TIMEOUT = "5s"


def configure_pg_session(connection: Any) -> None:
    """Fija la configuración de sesión obligatoria en una conexión nueva del pool.

    Se ejecuta en autocommit para que los `set_config` (.., false) SURTAN efecto
    en la sesión y no queden pendientes de un COMMIT posterior, y para no dejar
    una transacción implícita abierta que el primer `SELECT` de la aplicación
    heredaría. La conexión recién creada está en estado IDLE, por lo que volver
    a autocommit=False no puede perder trabajo previo.
    """
    connection.autocommit = True
    try:
        connection.execute("SELECT set_config('TimeZone', %s, false)", (PG_SESSION_TIMEZONE,))
        connection.execute("SELECT set_config('lock_timeout', %s, false)", (PG_LOCK_TIMEOUT,))
    finally:
        connection.autocommit = False


def sanitize_database_url(url: str | None) -> str | None:
    """Enmascara la contraseña en una URL/DSN de base de datos para logging o reporting.

    Ejemplo:
        postgresql://user:secret123@localhost:5432/turnobot
        -> postgresql://user:***@localhost:5432/turnobot
    """
    if not url:
        return None
    return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", url)


def normalize_database_url(url: str | None) -> str | None:
    """Normaliza prefijos de URL de conexión a PostgreSQL.

    Convierte postgres:// a postgresql:// para compatibilidad con libpq/psycopg3.
    """
    if not url:
        return None
    url_trimmed = url.strip()
    if url_trimmed.startswith("postgres://"):
        return "postgresql://" + url_trimmed[11:]
    return url_trimmed


def get_database_backend(url: str | None = None) -> str:
    """Determina el backend de base de datos a partir de la URL dada o del entorno.

    Retorna "postgresql" si la URL comienza con postgresql:// o postgres://,
    de lo contrario "sqlite".
    """
    normalized = normalize_database_url(url)
    if normalized and (
        normalized.startswith("postgresql://") or normalized.startswith("postgres://")
    ):
        return "postgresql"
    return "sqlite"


def create_pg_pool(
    conninfo: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    max_idle: float = 300.0,
    max_lifetime: float = 1800.0,
    timeout: float = 10.0,
    open: bool = True,
    **kwargs: Any,
) -> psycopg_pool.ConnectionPool:
    """Crea una instancia de ConnectionPool con los parámetros de diseño aprobados.

    Parámetros iniciales del diseño (Fase 4C):
        - min_size: 1
        - max_size: 10
        - max_idle: 300.0 (segundos)
        - max_lifetime: 1800.0 (segundos)
        - timeout: 10.0 (segundos para checkout)
    """
    normalized_conninfo = normalize_database_url(conninfo)
    if not normalized_conninfo:
        raise ValueError("conninfo no puede estar vacío para crear un pool de PostgreSQL")

    sanitized = sanitize_database_url(normalized_conninfo)
    logger.info(
        "Creando pool PostgreSQL para %s (min_size=%d, max_size=%d)", sanitized, min_size, max_size
    )

    # `configure` se invoca por el pool en CADA conexión nueva: es el punto
    # único donde se puede garantizar timezone/lock_timeout sin depender de
    # callers individuales. Un caller puede sustituirlo vía kwargs.
    kwargs.setdefault("configure", configure_pg_session)

    return psycopg_pool.ConnectionPool(
        conninfo=normalized_conninfo,
        min_size=min_size,
        max_size=max_size,
        max_idle=max_idle,
        max_lifetime=max_lifetime,
        timeout=timeout,
        open=open,
        **kwargs,
    )


def _current_flask_app() -> Flask | None:
    """Devuelve la aplicación Flask del contexto activo, o None si no hay contexto.

    `get_connection()` se llama desde servicios que no reciben la app (se
    resuelven vía `flask.current_app`), por lo que el pool debe poder localised
    sin que cada caller pase la app explícitamente.
    """
    try:
        from flask import current_app, has_app_context
    except ImportError:  # pragma: no cover - Flask siempre presente en runtime
        return None
    if not has_app_context():
        return None
    return current_app._get_current_object()  # type: ignore[attr-defined]


def _is_pool_open(pool: Any) -> bool:
    """True si hay un pool registrado y vigente.

    No consulta `pool.closed`: psycopg_pool marca como cerrado un pool creado con
    `open=False` (aún sin conexiones), y eso haría recrear pools en tests y CLI.
    El ciclo de vida lo gobierna `close_pg_pool()`, que además retira el pool de
    `app.extensions`: si sigue registrado, es el pool vigente.
    """
    return pool is not None


def init_pg_pool(
    app: Flask | None = None,
    conninfo: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
    max_idle: float = 300.0,
    max_lifetime: float = 1800.0,
    timeout: float = 10.0,
    open: bool = True,
) -> psycopg_pool.ConnectionPool | None:
    """Inicializa el pool de conexiones PostgreSQL para la aplicación Flask o a nivel global.

    Fuente de verdad: `app.extensions["pg_pool"]` cuando se pasa una app Flask.
    `_global_pool` guarda una referencia al MISMO objeto como respaldo para los
    paths que corren sin contexto Flask (`create_app()` → `init_database()` antes
    de atender el primer request, scripts CLI); nunca es un segundo pool.

    Es idempotente por app: si esa app ya tiene un pool registrado se lo devuelve
    sin crear otro, de modo que re-entradas no multiplican conexiones.
    """
    global _global_pool

    effective_url = conninfo
    if effective_url is None and app is not None:
        effective_url = app.config.get("DATABASE_URL")

    backend = get_database_backend(effective_url)
    if backend != "postgresql" or not effective_url:
        logger.debug("Backend actual es %s; no se inicializa pool de PostgreSQL", backend)
        return None

    if app is not None:
        existing = getattr(app, "extensions", {}).get("pg_pool")
        if _is_pool_open(existing):
            logger.debug("Pool PostgreSQL ya inicializado para esta app; se reutiliza")
            return existing

    pool = create_pg_pool(
        effective_url,
        min_size=min_size,
        max_size=max_size,
        max_idle=max_idle,
        max_lifetime=max_lifetime,
        timeout=timeout,
        open=open,
    )

    if app is not None:
        app.extensions = getattr(app, "extensions", {})
        app.extensions["pg_pool"] = pool

    # Respaldo para callers sin contexto Flask: misma instancia, no una copia.
    _global_pool = pool

    return pool


def get_pg_pool(app: Flask | None = None) -> psycopg_pool.ConnectionPool | None:
    """Obtiene el pool activo: app explícita > app del contexto Flask > respaldo global."""
    target_app = app if app is not None else _current_flask_app()
    if target_app is not None:
        pool = getattr(target_app, "extensions", {}).get("pg_pool")
        if pool is not None:
            return pool
    return _global_pool


def close_pg_pool(
    pool: psycopg_pool.ConnectionPool | None = None, app: Flask | None = None, timeout: float = 5.0
) -> None:
    """Cierra limpiamente un pool de conexiones PostgreSQL y limpia referencias.

    Es idempotente: cerrar un pool ya cerrado (o una app que nunca tuvo pool)
    no lanza. Cuando se pasa `app` el cierre se limita a ESA app: nunca cae al
    respaldo global, para que un teardown repetido no pueda cerrar el pool de
    otra instancia de la aplicación.
    """
    global _global_pool

    target_pool = pool
    if target_pool is None and app is not None:
        target_pool = getattr(app, "extensions", {}).pop("pg_pool", None)
    elif target_pool is None:
        target_pool = _global_pool

    if target_pool is not None:
        if target_pool is _global_pool:
            _global_pool = None
        try:
            logger.info("Cerrando pool PostgreSQL")
            target_pool.close(timeout=timeout)
        except Exception as err:
            logger.warning("Error al cerrar pool PostgreSQL: %s", err)


def register_pg_pool_teardown(app: Flask) -> Callable[[], None]:
    """Engancha el cierre del pool al fin de vida de la aplicación Flask.

    Flask no expone un evento de "aplicación apagada" para servidores WSGI
    reales (en producción se sirve con waitress tras systemd), por eso el
    teardown se registra en el hook `atexit` del proceso: se ejecuta al salir
    del proceso, incluido el salida ordenada por SIGTERM que usa
    `turnobot.service`. Deliberadamente NO se usa `teardown_appcontext`, que
    correría al final de CADA request/app context y cerraría el pool durante
    el tráfico.

    Devuelve el callable de cierre para poder ejecutarlo explícitamente
    (tests, apagados manuales) y desregistrarlo con `atexit.unregister`.
    """
    extensions = getattr(app, "extensions", None)
    if extensions is None:  # pragma: no cover - Flask siempre define extensions
        app.extensions = {}

    def _shutdown_pg_pool() -> None:
        close_pg_pool(app=app)

    atexit.register(_shutdown_pg_pool)
    logger.debug("Teardown del pool PostgreSQL registrado para el fin de la aplicación")
    return _shutdown_pg_pool


# ============================================================
# ADAPTACIÓN SQL Y ROW PROXIES (Fase 4F)
# ============================================================


def replace_placeholders(sql: str) -> str:
    """Reemplaza los placeholders '?' por '%s' únicamente fuera de literales de texto.

    Preserva los signos '?' dentro de comillas simples ('...') y dobles ("...").
    """
    result: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char == "'" and not in_double:
            in_single = not in_single
            result.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
        elif char == "?" and not in_single and not in_double:
            result.append("%s")
        else:
            result.append(char)
        i += 1
    return "".join(result)


def split_sql_statements(sql: str) -> list[str]:
    """Divide un script SQL en sentencias individuales.

    Respeta literales de texto (comillas simples), comentarios de línea
    (``--``) y comentarios de bloque (``/* */``).  Las sentencias se
    separan por ``;`` solo cuando no están dentro de un literal.
    """
    result: list[str] = []
    current: list[str] = []
    in_single = False
    i = 0
    n = len(sql)

    while i < n:
        char = sql[i]

        if char == "-" and not in_single and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue

        if char == "/" and not in_single and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue

        if char == "'" and not in_single:
            in_single = True
            current.append(char)
        elif char == "'" and in_single:
            if i + 1 < n and sql[i + 1] == "'":
                current.append("''")
                i += 1
            else:
                in_single = False
                current.append(char)
        elif char == ";" and not in_single:
            stmt = "".join(current).strip()
            if stmt:
                result.append(stmt)
            current = []
        else:
            current.append(char)

        i += 1

    stmt = "".join(current).strip()
    if stmt:
        result.append(stmt)

    return result


def escape_literal_percent(sql: str) -> str:
    """Duplica los '%' literales ('%' -> '%%') emulando el parser de psycopg3.

    psycopg3 exige que todo '%' fuera de un placeholder (%s/%b/%t) o de un
    escape ('%%') se duplique. De lo contrario lanza ProgrammingError al
    ejecutar consultas parametrizadas que contienen literales como
    `LIKE '%' || columna || '%'`. No afecta los placeholders ya generados
    ('%s') ni los pares de escape existentes ('%%').
    """
    result: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char == "%":
            if i + 1 < n and sql[i + 1] == "%":
                result.append("%%")
                i += 2
                continue
            if i + 1 < n and sql[i + 1] in ("s", "b", "t"):
                result.append(char)
                i += 1
                continue
            result.append("%%")
            i += 1
            continue
        result.append(char)
        i += 1
    return "".join(result)


def adapt_query_for_postgres(query: str, params: Any = None) -> tuple[str | None, Any]:
    """Adapta una sentencia SQL escrita para SQLite al dialecto PostgreSQL (Fase 4F).

    Realiza transformaciones necesarias:
    - Omite sentencias PRAGMA propias de SQLite.
    - Convierte 'BEGIN IMMEDIATE' a 'BEGIN'.
    - Convierte 'INSERT OR IGNORE INTO <table>' a 'INSERT INTO <table> ... ON CONFLICT DO NOTHING'.
    - Convierte 'INSERT OR REPLACE INTO schema_version ...' a 'INSERT INTO schema_version ... ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version'.
    - Convierte 'INSERT OR REPLACE INTO loyalty_settings ...' a 'INSERT INTO loyalty_settings ... ON CONFLICT (business_id) DO UPDATE SET ...'.
    - Convierte 'datetime('now')' a 'CURRENT_TIMESTAMP'.
    - Convierte 'datetime('now', ?)' a '(CURRENT_TIMESTAMP + (%s)::interval)'.
    - Reemplaza placeholders '?' por '%s'.
    - Duplica '%' literales ('%' -> '%%') para el parser de psycopg3.
    """
    if not query:
        return query, params

    trimmed = query.strip()
    uppercase_trimmed = trimmed.upper()

    # 1. Omite PRAGMA en PostgreSQL
    if uppercase_trimmed.startswith("PRAGMA"):
        return None, None

    # 2. Transformar BEGIN IMMEDIATE a BEGIN
    if uppercase_trimmed == "BEGIN IMMEDIATE":
        return "BEGIN", params

    sql = query

    # 3. Transformar INSERT OR IGNORE
    if "INSERT OR IGNORE INTO" in uppercase_trimmed:
        pattern = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
        sql = pattern.sub("INSERT INTO", sql)
        if "ON CONFLICT" not in sql.upper():
            sql = sql + " ON CONFLICT DO NOTHING"

    # 4. Transformar INSERT OR REPLACE / REPLACE INTO
    if "INSERT OR REPLACE INTO" in uppercase_trimmed or uppercase_trimmed.startswith(
        "REPLACE INTO"
    ):
        pattern = re.compile(
            r"(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE
        )
        match = pattern.search(sql)
        if match:
            tbl_name = match.group(1).lower()
            sql = pattern.sub(f"INSERT INTO {tbl_name}", sql)
            if "ON CONFLICT" not in sql.upper():
                if tbl_name == "schema_version":
                    sql = sql + " ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version"
                elif tbl_name == "loyalty_settings":
                    sql = (
                        sql
                        + " ON CONFLICT (business_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
                        "points_per_completed_appointment = EXCLUDED.points_per_completed_appointment, "
                        "updated_at = CURRENT_TIMESTAMP"
                    )
                else:
                    sql = sql + " ON CONFLICT DO NOTHING"

    # 5. Transformar datetime('now', ...) y datetime('now')
    if "datetime('now'" in sql or "DATETIME('NOW'" in sql:
        sql = re.sub(
            r"datetime\(\s*'now'\s*,\s*(\?|%s)\s*\)",
            r"(CURRENT_TIMESTAMP + (\1)::interval)",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(r"datetime\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)

    # 6. Transformar comparaciones booleanas SQLite (col = 1/0) a PostgreSQL (col / NOT col)
    # Se hace después del reemplazo de placeholders para no interferir con ? en otros contextos
    sql = _adapt_boolean_comparisons(sql)

    # 6b. Convertir 0/1 en INSERT VALUES de columnas BOOLEAN a FALSE/TRUE
    sql = _adapt_insert_boolean_values(sql)

    # 7. Reemplazar placeholders '?' por '%s' fuera de comillas
    sql = replace_placeholders(sql)

    # 8. Escapar '%' literales para el parser de psycopg3
    sql = escape_literal_percent(sql)

    return sql, params


def _adapt_boolean_comparisons(sql: str) -> str:
    """Convierte comparaciones booleanas estilo SQLite (col = 1 / col = 0) a PostgreSQL nativo.

    En SQLite: WHERE active = 1, WHERE active = 0, WHERE is_open = 1, etc.
    En PostgreSQL: WHERE active, WHERE NOT active, WHERE is_open, WHERE NOT is_open

    Esta transformación es segura porque:
    - Solo afecta comparaciones con literales 1 o 0
    - No afecta placeholders (? o %s) ni columnas numéricas reales
    - Las columnas BOOLEAN en PostgreSQL aceptan IS TRUE / IS FALSE / NOT col
    """
    import re

    # Lista de columnas conocidas como BOOLEAN en el esquema PostgreSQL
    boolean_columns = {
        "active",
        "pending",
        "is_open",
        "needs_human",
        "revoked",
        "enabled",
        "notifications_enabled",
    }

    # Patrón: columna = 1  o  columna = 0  (fuera de comillas)
    # Usamos word boundaries para evitar coincidencias parciales
    for col in boolean_columns:
        # col = 1  ->  col
        pattern_eq_1 = re.compile(rf"\b{re.escape(col)}\s*=\s*1\b", re.IGNORECASE)
        sql = pattern_eq_1.sub(col, sql)

        # col = 0  ->  NOT col
        pattern_eq_0 = re.compile(rf"\b{re.escape(col)}\s*=\s*0\b", re.IGNORECASE)
        sql = pattern_eq_0.sub(f"NOT {col}", sql)

        # col != 1  ->  NOT col  (poco común pero por completitud)
        pattern_ne_1 = re.compile(rf"\b{re.escape(col)}\s*(?:!=|<>)\s*1\b", re.IGNORECASE)
        sql = pattern_ne_1.sub(f"NOT {col}", sql)

        # col != 0  ->  col
        pattern_ne_0 = re.compile(rf"\b{re.escape(col)}\s*(?:!=|<>)\s*0\b", re.IGNORECASE)
        sql = pattern_ne_0.sub(col, sql)

    return sql


def _adapt_insert_boolean_values(sql: str) -> str:
    """Convierte literales 0/1 en columnas BOOLEAN de INSERT VALUES a TRUE/FALSE.

    PostgreSQL rechaza 'column "enabled" is of type boolean but expression is of type integer'.
    En SQLite se usa 0/1 para booleanos; en INSERT VALUES se debe convertir explícitamente
    al tipo BOOLEAN de PostgreSQL (0 -> false, 1 -> true) cuando la columna es BOOLEAN.
    """
    boolean_columns = {
        "active",
        "pending",
        "is_open",
        "needs_human",
        "revoked",
        "enabled",
        "notifications_enabled",
    }

    import re

    pattern = re.compile(
        r"(INSERT\s+INTO\s+\w+\s*\()\s*([^)]+)\s*(\)\s*VALUES\s*\()", re.IGNORECASE
    )
    match = pattern.search(sql)
    if not match:
        return sql

    columns_str = match.group(2)
    columns = [c.strip().strip('"').strip("'") for c in columns_str.split(",")]
    values_start = match.end()
    depth = 1
    i = values_start
    while i < len(sql) and depth > 0:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    values_str = sql[values_start : i - 1]

    values = []
    current = ""
    in_single = False
    for char in values_str:
        if char == "'" and not in_single:
            in_single = True
            current += char
        elif char == "'" and in_single:
            if current and current[-1] == "'":
                current += char
            else:
                in_single = False
                current += char
        elif char == "," and not in_single:
            values.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        values.append(current.strip())

    if len(columns) != len(values):
        return sql

    for idx, col in enumerate(columns):
        if col.lower() in boolean_columns:
            val = values[idx].strip()
            if val == "0":
                values[idx] = "false"
            elif val == "1":
                values[idx] = "true"

    new_values_str = ", ".join(values)
    return sql[:values_start] + new_values_str + sql[i - 1 :]


class PgRowProxy(dict):
    """Objeto fila que imita el comportamiento de sqlite3.Row en PostgreSQL.

    Proporciona acceso por nombre de columna (row["id"]), por índice numérico
    (row[0]), conversión a diccionario (dict(row)) y métodos dict (.get(), .keys()).
    """

    __slots__ = ("_keys", "_values")

    def __init__(self, keys: tuple[str, ...], values: tuple[Any, ...]) -> None:
        super().__init__(zip(keys, values))
        self._keys = keys
        self._values = values

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, int):
            return self._values[item]
        return super().__getitem__(item)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def keys(self) -> tuple[str, ...]:  # type: ignore[override]
        return self._keys

    def values(self) -> tuple[Any, ...]:  # type: ignore[override]
        return self._values

    def items(self) -> zip:  # type: ignore[override]
        return zip(self._keys, self._values)


def _to_pg_row_proxy(row: Any, description: Any) -> PgRowProxy | Any:
    if row is None:
        return None
    if isinstance(row, PgRowProxy):
        return row

    if description:
        keys = tuple(col[0] for col in description)
        if isinstance(row, (tuple, list)):
            values = tuple(row)
        elif isinstance(row, dict):
            values = tuple(row.get(k) for k in keys)
        else:
            values = tuple(row)
        return PgRowProxy(keys, values)

    if isinstance(row, dict):
        keys = tuple(row.keys())
        values = tuple(row.values())
        return PgRowProxy(keys, values)

    return row


# ============================================================
# COMANDOS DE TRANSACCIÓN Y TRADUCCIÓN DE ERRORES (Fase 4G)
#
# SQLite se apoya en `BEGIN IMMEDIATE` para serializar escritores. psycopg3
# gestiona la transacción por sí mismo y rechaza un `BEGIN` manual cuando ya
# hay una en curso ("there is already a transaction in progress"). Para
# preservar la SEMÁNTICA de negocio (no la implementación interna), estos
# helpers traducen los comandos de transacción que el código de la aplicación
# emite vía execute() y mapean los errores psycopg3 a las excepciones sqlite3
# que los puntos de captura existentes ya esperan.
# ============================================================


def _pg_transaction_state(connection):
    """Devuelve el estado transaccional psycopg3 de una conexión (o None)."""
    try:
        return connection.info.transaction_status
    except Exception:
        return None


def _pg_is_in_transaction(connection):
    status = _pg_transaction_state(connection)
    if status is None or _PgTransactionStatus is None:
        return False
    return status in (_PgTransactionStatus.INTRANS, _PgTransactionStatus.ACTIVE)


def _start_pg_transaction(connection) -> None:
    """Abre la transacción implícita de psycopg3 si no hay una en curso.

    psycopg3 (a diferencia de psycopg2) NO expone ``Connection.begin()``: con
    autocommit desactivado la transacción se abre IMPLÍCITAMENTE en la primera
    ejecución contra el servidor. Para conservar la semántica del seam
    (``BEGIN IMMEDIATE`` debe dejar una transacción activa) se dispara esa
    transacción implícita mediante una sentencia benigna de solo lectura.
    En autocommit no hay transacción que abrir y no se ejecuta nada.
    """
    if getattr(connection, "autocommit", False):
        return
    connection.execute("SELECT 1")


def handle_transaction_command(connection, query):
    """Traduce comandos de transacción SQLite a los métodos de psycopg3.

    Devuelve True si ``query`` era un comando de transacción (BEGIN IMMEDIATE,
    BEGIN, COMMIT, ROLLBACK) y quedó manejado por la conexión psycopg3:

    - ``BEGIN IMMEDIATE``: abre transacción solo si no hay una en curso. Si una
      lectura previa ya abrió una implícita (patrón read-then-write), es no-op
      y evita el error "already in a transaction in progress".
    - ``ROLLBACK``: ``connection.rollback()`` solo si hay transacción activa.
    - ``COMMIT``: ``connection.commit()`` solo si hay transacción activa.
    """
    command = (query or "").strip().upper()
    if command not in ("BEGIN IMMEDIATE", "BEGIN", "COMMIT", "ROLLBACK"):
        return False

    if command == "BEGIN IMMEDIATE":
        status = _pg_transaction_state(connection)
        if _PgTransactionStatus is not None and status == _PgTransactionStatus.INERROR:
            connection.rollback()
        if not _pg_is_in_transaction(connection):
            _start_pg_transaction(connection)
        return True

    if command == "COMMIT":
        if _pg_is_in_transaction(connection):
            connection.commit()
        return True

    if command == "ROLLBACK":
        status = _pg_transaction_state(connection)
        if _PgTransactionStatus is not None and status in (
            _PgTransactionStatus.INTRANS,
            _PgTransactionStatus.ACTIVE,
            _PgTransactionStatus.INERROR,
        ):
            connection.rollback()
        elif _pg_is_in_transaction(connection):
            connection.rollback()
        return True

    return False


def translate_pg_error(error):
    """Mapea errores psycopg3 a las excepciones equivalentes de sqlite3.

    El código de la aplicación captura ``sqlite3.IntegrityError`` /
    ``sqlite3.OperationalError`` en decenas de puntos (reserva ocupada,
    idempotencia de puntos, rewards duplicados, tokens ya usados, migraciones).
    psycopg3 lanza sus propios ``psycopg.errors.*``, incompatibles con ese
    contrato. Traducirlos aquí hace que esas ramas funcionen sin tocar la capa
    de negocio ni en SQLite ni en PostgreSQL.
    """
    if isinstance(error, sqlite3.Error):
        return error
    mapping = (
        (sqlite3.IntegrityError, _psycopg_errors.IntegrityError),
        (sqlite3.OperationalError, _psycopg_errors.OperationalError),
        (sqlite3.ProgrammingError, _psycopg_errors.ProgrammingError),
        (sqlite3.InternalError, _psycopg_errors.InternalError),
        (sqlite3.NotSupportedError, _psycopg_errors.NotSupportedError),
    )
    for sqlite_cls, psycopg_error_cls in mapping:
        try:
            if isinstance(error, psycopg_error_cls):
                return sqlite_cls(str(error))
        except TypeError:
            continue
    if isinstance(error, psycopg.Error):
        return sqlite3.DatabaseError(str(error))
    return error


def _call_translated(call):
    """Ejecuta un callable psycopg y traduce sus errores a sqlite3.*."""
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - traducción deliberada de errores
        translated = translate_pg_error(error)
        if translated is error:
            raise
        raise translated from error


class PgCursorProxy:
    """Cursor proxy para psycopg3 que adapta la ejecución de consultas SQLite a PostgreSQL,
    emula sqlite3.Row mediante PgRowProxy y gestiona lastrowid.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._lastrowid: int | None = None

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    def execute(self, query: str, params: Any = None) -> PgCursorProxy:
        if handle_transaction_command(self._cursor.connection, query):
            self._lastrowid = None
            return self

        adapted_query, adapted_params = adapt_query_for_postgres(query, params)
        if adapted_query is None:
            self._lastrowid = None
            return self

        is_insert = adapted_query.strip().upper().startswith("INSERT")
        if is_insert and "RETURNING" not in adapted_query.upper():
            adapted_query += " RETURNING id"

        _call_translated(
            lambda: (
                self._cursor.execute(adapted_query)
                if adapted_params is None
                else self._cursor.execute(adapted_query, adapted_params)
            )
        )

        if is_insert:
            try:
                res = self._cursor.fetchone()
                if res:
                    if isinstance(res, dict):
                        self._lastrowid = res.get("id") or next(iter(res.values()), None)
                    elif isinstance(res, (tuple, list)):
                        self._lastrowid = res[0]
            except Exception:
                self._lastrowid = None
        return self

    def fetchone(self) -> PgRowProxy | Any:
        res = self._cursor.fetchone()
        if res is None:
            return None
        return _to_pg_row_proxy(res, self._cursor.description)

    def fetchall(self) -> list[PgRowProxy | Any]:
        rows = self._cursor.fetchall()
        desc = self._cursor.description
        return [_to_pg_row_proxy(r, desc) for r in rows]

    def executemany(self, query: str, seq_of_params: Any) -> PgCursorProxy:
        adapted_query, _ = adapt_query_for_postgres(query, None)
        if adapted_query is None:
            return self
        _call_translated(lambda: self._cursor.executemany(adapted_query, seq_of_params))
        return self

    def executescript(self, sql_script: str) -> PgCursorProxy:
        adapted_script, _ = adapt_query_for_postgres(sql_script, None)
        if adapted_script:
            statements = split_sql_statements(adapted_script)
            for stmt in statements:
                stmt = stmt.strip()
                if not stmt:
                    continue
                _call_translated(lambda s=stmt: self._cursor.execute(s))
        return self

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class PgConnectionProxy:
    """Envoltura de psycopg_connection para adaptar con el contrato
    sqlite3.Connection usado por TurnoGo (close -> putconn, execute, cursor).
    """

    __slots__ = ("_conn", "_pool", "_returned")

    def __init__(self, conn: Any, pool: psycopg_pool.ConnectionPool) -> None:
        self._conn = conn
        self._pool = pool
        self._returned = False

    def cursor(self) -> PgCursorProxy:
        return PgCursorProxy(self._conn.cursor())

    def execute(self, query: str, params: Any = None) -> PgCursorProxy:
        if handle_transaction_command(self._conn, query):
            return PgCursorProxy(self._conn.cursor())
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        try:
            self._pool.putconn(self._conn)
        except Exception as err:
            logger.warning("putconn falló, cerrando conexión: %s", err)
            try:
                self._conn.close()
            except Exception:
                pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def get_pg_pool_connection(app: Flask | None = None) -> PgConnectionProxy:
    """Checkout seguro de una conexión del pool existente.

    Retorna un PgConnectionProxy que devolverá la conexión al pool al cerrar.
    Requiere que init_pg_pool() haya sido invocado (o app con extension 'pg_pool').
    """
    pool = get_pg_pool(app)
    if pool is None:
        raise RuntimeError(
            "El pool PostgreSQL no está inicializado. "
            "create_app() debe invocar init_pg_pool() antes de get_connection()."
        )
    return PgConnectionProxy(pool.getconn(), pool)
