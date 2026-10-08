"""Módulo de infraestructura y ciclo de vida del Pool de Conexiones PostgreSQL (Fase 4C - 4F).

Proporciona abstracción mínima para la inicialización, acceso y cierre
del pool de conexiones psycopg_pool.ConnectionPool, así como proxies de conexión,
cursor y filas (PgRowProxy, PgCursorProxy, PgConnectionProxy) para adaptar la sintaxis
y acceso de datos de SQLite a PostgreSQL sin alterar la funcionalidad sobre SQLite.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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
# True si `_global_pool` fue registrado por una app Flask. Un pool propiedad de una
# app viva NO está huérfano aunque deje de ser el respaldo global —esa app lo
# referencia en sus `extensions` y lo cierra en su teardown—, mientras que uno
# creado sin app solo vive en esta variable. Esa diferencia decide si al
# reemplazarlo hay que cerrarlo. Se guarda un booleano y no la app para no
# retenerla aquí.
_global_pool_app_owned: bool = False


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

# Columnas BOOLEAN del esquema PostgreSQL (ver migrations_pg/001_initial_schema.sql).
# El esquema las declara BOOLEAN mientras que el código de aplicación —heredado de
# SQLite— las manipula con los literales enteros 0/1. Vive en UN solo lugar porque las
# tres adaptaciones que dependen de esta lista (predicados, asignaciones SET y
# parámetros) deben coincidir exactamente: si divergen, se genera SQL inválido.
_BOOLEAN_COLUMNS = frozenset(
    {"active", "pending", "is_open", "needs_human", "revoked", "enabled", "notifications_enabled"}
)


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


def resolve_database_backend(
    url: str | None = None, *, environment: str | None = None, sqlite_opt_in: str | None = None
) -> tuple[str, str | None]:
    """Valida una única selección de backend y devuelve ``(backend, URL normalizada)``.

    En producción solo se permite una URL PostgreSQL completa. En desarrollo y
    tests SQLite requiere ``DB_BACKEND=sqlite`` explícito; no hay fallback.
    """
    environment = environment if environment is not None else os.getenv("FLASK_ENV")
    sqlite_opt_in = sqlite_opt_in if sqlite_opt_in is not None else os.getenv("DB_BACKEND")
    normalized = normalize_database_url(url)
    if not normalized:
        if environment == "production":
            raise RuntimeError(
                "DATABASE_URL es obligatoria y debe apuntar a PostgreSQL en producción"
            )
        if sqlite_opt_in == "sqlite":
            return "sqlite", None
        raise RuntimeError("DATABASE_URL PostgreSQL o DB_BACKEND=sqlite explícito son obligatorios")

    scheme = urlsplit(normalized).scheme.lower()
    if scheme not in ("postgres", "postgresql"):
        if scheme == "sqlite" and environment != "production" and sqlite_opt_in == "sqlite":
            return "sqlite", normalized
        if environment == "production":
            raise RuntimeError(
                "DATABASE_URL de producción debe usar PostgreSQL; SQLite no está permitido"
            )
        raise RuntimeError(
            "DATABASE_URL debe usar PostgreSQL; SQLite requiere DB_BACKEND=sqlite explícito"
        )

    try:
        parsed = urlsplit(normalized)
        if not parsed.hostname or not parsed.path or parsed.path == "/":
            raise ValueError("host y nombre de base requeridos")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("puerto inválido")
    except ValueError as error:
        raise RuntimeError("DATABASE_URL PostgreSQL malformada") from error
    return "postgresql", normalized


def get_database_backend(url: str | None = None) -> str:
    """Compatibilidad: retorna el backend validado por el resolver único."""
    if url is None:
        url = os.getenv("DATABASE_URL")
    return resolve_database_backend(url)[0]


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


def _pool_conninfo(pool: Any) -> str | None:
    """Conninfo con el que se construyó un pool, o None si no se puede determinar.

    Lo usan los dobles de test (`_FakePool`) y cualquier pool que no lo exponga, para
    no asumir que dos pools son el mismo solo porque ambos existen.
    """
    conninfo = getattr(pool, "conninfo", None)
    return conninfo if isinstance(conninfo, str) else None


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

    Sin app (`app=None`) el pool vigente es `_global_pool`, y también es idempotente
    para el MISMO conninfo: cada llamada devolvía antes un pool nuevo que pisaba la
    variable sin cerrar el anterior, así que una re-entrada por el mismo URL dejaba
    un pool huérfano con hasta `max_size` conexiones abiertas en el servidor y sin
    ninguna referencia que lo cerrara. Si el conninfo es OTRO, el pool previo se
    cierra aquí salvo que pertenezca a una app (esa app lo cierra en su teardown y
    cerrarlo desde aquí le rompería el servicio).
    """
    global _global_pool, _global_pool_app_owned

    effective_url = conninfo
    if effective_url is None and app is not None:
        effective_url = app.config.get("DATABASE_URL")

    if app is not None and app.config.get("DB_BACKEND"):
        backend = app.config["DB_BACKEND"]
    else:
        backend = get_database_backend(effective_url)
    if backend != "postgresql" or not effective_url:
        logger.debug("Backend actual es %s; no se inicializa pool de PostgreSQL", backend)
        return None

    if app is not None:
        existing = getattr(app, "extensions", {}).get("pg_pool")
        if _is_pool_open(existing):
            logger.debug("Pool PostgreSQL ya inicializado para esta app; se reutiliza")
            return existing
    else:
        existing = _global_pool
        if existing is not None and not _global_pool_app_owned:
            # Solo se reutiliza un pool SIN dueño. Uno propiedad de una app no se cede:
            # sigue registrado en sus `extensions` y el caller de este path no lo pidió
            # explícitamente, así que si se lo devolviera y luego lo cerrara (el
            # contrato de los scripts CLI) le rompería el servicio a la app.
            if _pool_conninfo(existing) == normalize_database_url(effective_url):
                logger.debug(
                    "Pool PostgreSQL global ya inicializado para este conninfo; se reutiliza"
                )
                return existing
            # Distinto conninfo: al dejar de ser el respaldo global nadie lo cerraría.
            logger.info("Cambia el conninfo del pool global; se cierra el pool anterior")
            close_pg_pool(existing)

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
    _global_pool_app_owned = app is not None

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
    global _global_pool, _global_pool_app_owned

    target_pool = pool
    if target_pool is None and app is not None:
        target_pool = getattr(app, "extensions", {}).pop("pg_pool", None)
    elif target_pool is None:
        target_pool = _global_pool

    if target_pool is not None:
        if target_pool is _global_pool:
            _global_pool = None
            _global_pool_app_owned = False
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
    - Transforma comparaciones booleanas SQLite (col = 1/0) a PostgreSQL (col IS TRUE / NOT col).
    - Corrige placeholders '?' en patrones `(? IS NULL OR col = ?)` para evitar 'could not determine data type of parameter'.
    - Convierte literales 0/1 en columnas BOOLEAN de INSERT VALUES a FALSE/TRUE.
    - Convierte params 0/1 para columnas BOOLEAN en UPDATE SET a Python bool.
    - Reemplaza placeholders '?' por '%s' fuera de comillas.
    - Duplica '%' literales para el parser de psycopg3.
    """
    if not query:
        return query, params

    trimmed = query.strip()
    uppercase_trimmed = trimmed.upper()

    # 1. Omite PRAGMA propias de SQLite
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
    sql = _adapt_boolean_comparisons(sql)

    # 6b. Fix para 'could not determine data type of parameter' en patrones comunes
    # (? IS NULL OR col = ?)  ->  (col IS NULL OR col = ?)
    # En SQLite, ? IS NULL funciona con cualquier tipo. En PostgreSQL, un placeholder
    # sin tipo no puede usarse con IS NULL. Reescribimos para inferir el tipo.
    sql = _adapt_nullable_placeholders(sql)

    # 6c. Convertir 0/1 en INSERT VALUES de columnas BOOLEAN a FALSE/TRUE
    sql, params = _adapt_insert_boolean_values(sql, params)

    # 6d. Convertir params 0/1 para columnas BOOLEAN en UPDATE SET statements
    sql, params = _adapt_update_boolean_params(sql, params)

    # 6e. Convertir params 0/1 ligados a comparaciones de columnas BOOLEAN.
    params = _adapt_boolean_predicate_params(sql, params)

    # 7. Reemplazar placeholders '?' por '%s' fuera de comillas
    sql = replace_placeholders(sql)

    # 8. Escapar '%' literales para el parser de psycopg3
    sql = escape_literal_percent(sql)

    return sql, params


def _adapt_nullable_placeholders(sql: str) -> str:
    """Rewrites `(? IS NULL OR col = ?)` patterns to avoid PostgreSQL's
    'could not determine data type of parameter' error.

    In SQLite, `?` used with `IS NULL` works because SQLite uses dynamic typing.
    PostgreSQL needs the parameter type to be inferrable. We cast the placeholder
    to TEXT to allow PostgreSQL to infer a type, making `IS NULL` valid.

    Pattern: `? IS NULL OR col = ?` -> `(?::text IS NULL OR col = ?)
    (Note: uses PostgreSQL ::text cast syntax)
    """
    import re

    # (? IS NULL OR col = ?) -> (CAST(? AS TEXT) IS NULL OR col = ?)
    pattern = re.compile(r"\(\s*\?\s*IS\s+NULL\s+OR\s+(\w+)\s*=\s*\?\s*\)", re.IGNORECASE)
    sql = pattern.sub(r"(CAST(? AS TEXT) IS NULL OR \1 = ?)", sql)
    return sql


def _scan_single_quoted(sql: str, start: int, backslash_escapes: bool = False) -> int:
    """Devuelve el indice justo despues del literal que empieza en `start`.

    `start` debe apuntar a la comilla de apertura. Una comilla simple duplicada
    (`''`) es el escape estandar de SQL y NO cierra el literal; en las cadenas con
    prefijo E/U& tambien cuenta la barra invertida como escape.
    """
    n = len(sql)
    i = start + 1
    while i < n:
        char = sql[i]
        if backslash_escapes and char == "\\":
            i += 2
            continue
        if char == "'":
            if i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _protected_sql_spans(sql: str) -> list[tuple[int, int]]:
    """Localiza los tramos de `sql` que NO son codigo SQL ejecutable.

    Devuelve spans (inicio, fin) de literales de texto, cadenas delimitadas por
    dolares y comentarios. Las comillas dobles NO se incluyen: encierran
    identificadores, que si son codigo y deben seguir adaptandose.

    Las tres adaptaciones booleanas buscan con regex `col = 1` / `col = 0`. Sin
    este filtro, un texto como `'active = 1'` (un valor legitimo que el usuario
    puede guardar, o una comparacion de strings) se reescribiria a
    `'active IS TRUE'`: en el mejor caso cambia los datos, en el peor produce SQL
    invalido. El escape por comilla duplicada es justamente lo que rompe un
    escaner ingenuo que abra con `'` y cierre con el siguiente `'`.
    """
    import re

    spans: list[tuple[int, int]] = []
    n = len(sql)
    i = 0
    while i < n:
        char = sql[i]
        if char == "'":
            end = _scan_single_quoted(sql, i)
            spans.append((i, end))
            i = end
            continue
        if char in "eE" and sql[i + 1 : i + 2] == "'":
            end = _scan_single_quoted(sql, i + 1, backslash_escapes=True)
            spans.append((i, end))
            i = end
            continue
        if sql[i : i + 3].upper() == "U&'":
            end = _scan_single_quoted(sql, i + 2, backslash_escapes=True)
            spans.append((i, end))
            i = end
            continue
        if char == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if sql[i + 1 : i + 2] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if char == "$":
            tag = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", sql[i:])
            if tag:
                closing = sql.find(tag.group(0), i + len(tag.group(0)))
                if closing != -1:
                    end = closing + len(tag.group(0))
                    spans.append((i, end))
                    i = end
                    continue
        if sql[i : i + 2] == "--":
            newline = sql.find("\n", i)
            end = n if newline == -1 else newline
            spans.append((i, end))
            i = end
            continue
        if sql[i : i + 2] == "/*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif sql[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            spans.append((i, j))
            i = j
            continue
        i += 1
    return spans


def _mask_sql_literals(sql: str) -> tuple[str, dict[str, str]]:
    """Sustituye cada tramo protegido por un token opaco, para adaptar solo el codigo.

    Devuelve el SQL enmascarado y el mapa token -> tramo original. El token usa
    caracteres de palabra, de modo que los limites de palabra de las regex siguen
    siendo validos, y su nombre no colisiona con ninguna columna booleana.
    """
    spans = _protected_sql_spans(sql)
    if not spans:
        return sql, {}
    pieces: list[str] = []
    mapping: dict[str, str] = {}
    last = 0
    for index, (start, end) in enumerate(spans):
        pieces.append(sql[last:start])
        token = f"__sqllit{index}__"
        pieces.append(token)
        mapping[token] = sql[start:end]
        last = end
    pieces.append(sql[last:])
    return "".join(pieces), mapping


def _transform_sql_code(sql: str, transform) -> str:
    """Aplica `transform` unicamente al codigo SQL y restaura los literales intactos."""
    masked, mapping = _mask_sql_literals(sql)
    if not mapping:
        return transform(masked)
    transformed = transform(masked)
    for token, original in mapping.items():
        transformed = transformed.replace(token, original)
    return transformed


def _blank_sql_literals(sql: str) -> str:
    """Copia de `sql` con los tramos protegidos sustituidos por espacios.

    Conserva la longitud exacta, de modo que los indices hallados sobre la copia
    son validos tambien sobre el SQL original. Se usa para localizar palabras
    clave (SET/WHERE) o contar asignaciones sin que un literal las falsee.
    """
    spans = _protected_sql_spans(sql)
    if not spans:
        return sql
    chars = list(sql)
    for start, end in spans:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _rewrite_boolean_predicates(sql: str) -> str:
    """Convierte comparaciones booleanas estilo SQLite (col = 1 / col = 0) a predicados PostgreSQL.

    En SQLite: WHERE active = 1, WHERE active = 0, WHERE is_open = 1, etc.
    En PostgreSQL: WHERE active IS TRUE, WHERE NOT active, WHERE is_open IS TRUE, etc.

    Se usa 'IS TRUE' en lugar de 'col' por sí solo porque PostgreSQL no puede
    inferir el tipo de placeholders ? en la misma cláusula WHERE cuando se usa
    una columna booleana sin operador de comparación explícito.

    NO debe aplicarse a una cláusula SET: allí `col = 1` es una ASIGNACIÓN y la
    forma correcta es `col = TRUE` (ver _adapt_boolean_assignments).

    Solo se adapta el código SQL: un literal como `'active = 1'` es un valor de
    texto y debe llegar intacto a PostgreSQL.
    """
    import re

    # `NOT` debe preceder a la columna COMPLETA (calificador incluido): una
    # sustitución que solo reemplaza el nombre deja `a.NOT active`, que es SQL
    # inválido. Para las formas `col IS TRUE` el calificador queda fuera del
    # match y se preserva solo, por eso no necesitan este tratamiento.
    def _not_predicate(col):
        def replace(match):
            qualifier = f"{match.group(1)}." if match.group(1) else ""
            return f"NOT {qualifier}{col}"

        return replace

    def adapt(sql_code: str) -> str:
        for col in _BOOLEAN_COLUMNS:
            # col = 1  ->  col IS TRUE
            sql_code = re.sub(
                rf"\b{re.escape(col)}\s*=\s*1\b", f"{col} IS TRUE", sql_code, flags=re.IGNORECASE
            )

            # [alias.]col = 0  ->  NOT [alias.]col
            sql_code = re.sub(
                rf"(?:(\w+)\.)?\b{re.escape(col)}\s*=\s*0\b",
                _not_predicate(col),
                sql_code,
                flags=re.IGNORECASE,
            )

            # [alias.]col != 1  ->  NOT [alias.]col  (poco común pero por completitud)
            sql_code = re.sub(
                rf"(?:(\w+)\.)?\b{re.escape(col)}\s*(?:!=|<>)\s*1\b",
                _not_predicate(col),
                sql_code,
                flags=re.IGNORECASE,
            )

            # col != 0  ->  col IS TRUE
            sql_code = re.sub(
                rf"\b{re.escape(col)}\s*(?:!=|<>)\s*0\b",
                f"{col} IS TRUE",
                sql_code,
                flags=re.IGNORECASE,
            )

        return sql_code

    return _transform_sql_code(sql, adapt)


def _adapt_boolean_assignments(sql: str) -> str:
    """Convierte literales 1/0 asignados a columnas BOOLEAN dentro de una cláusula SET.

    `SET revoked = 1` -> `SET revoked = TRUE`; `SET revoked = 0` -> `SET revoked = FALSE`.

    Una asignación NO admite la forma de predicado: PostgreSQL rechaza
    `SET col IS TRUE` (sintaxis) y `SET col = 1` (el entero no castea a boolean),
    de modo que la única forma válida es `SET col = <TRUE|FALSE>`.

    Solo se adapta el código SQL: un literal como `'active = 1'` dentro de un
    `SET nota = '...'` es un valor de texto y debe llegar intacto.
    """
    import re

    def adapt(sql_code: str) -> str:
        for col in _BOOLEAN_COLUMNS:
            sql_code = re.sub(
                rf"\b({re.escape(col)})\s*=\s*1\b", r"\1 = TRUE", sql_code, flags=re.IGNORECASE
            )
            sql_code = re.sub(
                rf"\b({re.escape(col)})\s*=\s*0\b", r"\1 = FALSE", sql_code, flags=re.IGNORECASE
            )
        return sql_code

    return _transform_sql_code(sql, adapt)


def _update_set_clause_span(sql: str) -> tuple[int, int] | None:
    """Localiza la cláusula SET de un UPDATE y la separa del resto de la sentencia.

    Devuelve (inicio, fin) del span que contiene la lista de asignaciones, desde
    la palabra clave SET hasta el inicio del WHERE siguiente, o hasta el final de
    la sentencia si no hay WHERE. Devuelve None si la sentencia no tiene SET.

    Solo se considera SET en un UPDATE (o en el `DO UPDATE` de un ON CONFLICT):
    un SELECT puede contener la palabra "SET" dentro de un literal y no debe
    arrastrar una adaptación de asignaciones.
    """
    import re

    # Las palabras clave se buscan sobre una copia con literales y comentarios en
    # blanco: `SET nota = 'where active = 0'` no contiene un WHERE real, y
    # `SELECT '-- set x'` no contiene un SET real. La copia conserva la longitud,
    # asi que los spans calculados sirven sobre el SQL original.
    code = _blank_sql_literals(sql)
    statement = code.lstrip()
    is_update = statement[:6].upper() == "UPDATE" or bool(
        re.search(r"\bDO\s+UPDATE\b", statement, re.IGNORECASE)
    )
    if not is_update:
        return None

    set_match = re.search(r"\bSET\b", code, re.IGNORECASE)
    if set_match is None:
        return None

    where_match = re.search(r"\bWHERE\b", code[set_match.end() :], re.IGNORECASE)
    end = set_match.end() + where_match.start() if where_match else len(sql)
    return set_match.start(), end


def _adapt_boolean_comparisons(sql: str) -> str:
    """Adapta los literales booleanos 1/0 de una sentencia al dialecto PostgreSQL.

    La conversión depende del CONTEXTO, y es lo que hace falta para no generar SQL
    inválido:

    - Predicados (WHERE/ON/JOIN): `col = 1` -> `col IS TRUE`, `col = 0` -> `NOT col`.
      Necesario porque PostgreSQL no define el operador `boolean = integer`.
    - Asignaciones (SET de un UPDATE): `col = 1` -> `col = TRUE`, `col = 0` -> `col = FALSE`.
      Aquí la forma de predicado sería un error de SINTAXIS (`SET col IS TRUE`) y
      dejar el `1` sin castear también lo sería (`boolean = integer`).

    Antes esta función aplicaba el mismo predicado a toda la sentencia, lo que
    producía `UPDATE sessions SET revoked IS TRUE ...`: sintaxis inválida que solo
    se manifiesta contra un servidor PostgreSQL real (con un cursor Mago/MagicMock
    nunca se parsea la sentencia).
    """
    set_span = _update_set_clause_span(sql)
    if set_span is None:
        return _rewrite_boolean_predicates(sql)

    start, end = set_span
    return (
        _rewrite_boolean_predicates(sql[:start])
        + _adapt_boolean_assignments(sql[start:end])
        + _rewrite_boolean_predicates(sql[end:])
    )


def _is_sql_placeholder(value: str) -> bool:
    """True si `value` es un placeholder posicional de psycopg3 (`?`, `%s`, `$n`)."""
    import re

    return value in ("?", "%s") or re.match(r"^\$\d+$", value) is not None


def _to_pg_bool(value: Any) -> Any:
    """Normaliza 0/1 a False/True; cualquier otro valor se devuelve intacto."""
    if value == 0 or value is False:
        return False
    if value == 1 or value is True:
        return True
    return value


def _split_top_level_items(text: str) -> list[str]:
    """Divide `text` por las comas de primer nivel, sin romper literales ni parentesis.

    Un `VALUES` puede contener expresiones anidadas (`COALESCE(a, b)`) y literales
    con comas o parentesis dentro (`'a,b'`, `'x)'`); cortar por comas a secas
    desalinearia los valores. El escape SQL de comilla duplicada (`'O''Brien'`)
    se tiene en cuenta para no cerrar el literal antes de tiempo.
    """
    items: list[str] = []
    current = ""
    in_single = False
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if in_single:
            current += char
            if char == "'":
                if i + 1 < n and text[i + 1] == "'":
                    current += text[i + 1]
                    i += 2
                    continue
                in_single = False
        elif char == "'":
            in_single = True
            current += char
        elif char == "(":
            depth += 1
            current += char
        elif char == ")":
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
        i += 1
    if current.strip():
        items.append(current.strip())
    return items


def _scan_values_tuples(sql: str, start: int) -> list[tuple[int, int]]:
    """Localiza todos los tuples de primer nivel del `VALUES`, como (inicio, fin) inclusives.

    Devuelve los spans de cada fila, de modo que el texto posterior (`RETURNING`,
    `ON CONFLICT`, ...) queda intacto. Un `VALUES` de una sola fila devuelve un
    unico span, igual que la extraccion anterior.
    """
    tuples: list[tuple[int, int]] = []
    i = start
    n = len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
            continue
        if sql[i] != "(":
            break
        depth = 0
        in_single = False
        j = i
        while j < n:
            char = sql[j]
            if in_single:
                if char == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    in_single = False
            elif char == "'":
                in_single = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            break
        tuples.append((i, j))
        i = j + 1
        while i < n and sql[i].isspace():
            i += 1
        if i < n and sql[i] == ",":
            i += 1
            continue
        break
    return tuples


def _adapt_insert_boolean_values(sql: str, params: Any = None) -> tuple[str, Any]:
    """Convierte literales 0/1 en columnas BOOLEAN de INSERT VALUES a TRUE/FALSE.

    PostgreSQL rechaza 'column "enabled" is of type boolean but expression is of type integer'.
    En SQLite se usa 0/1 para booleanos; en INSERT VALUES se debe convertir explícitamente
    al tipo BOOLEAN de PostgreSQL (0 -> false, 1 -> true) cuando la columna es BOOLEAN.

    Funciona tanto con SQL que tiene valores literales como con consultas parametrizadas
    (adapta los parámetros en `params` solo para posiciones que usan placeholders).

    Cubre TODAS las filas de un `VALUES (...), (...)`: antes solo se adaptaba el primer
    tuple, de modo que las filas 2..N conservaban el entero y PostgreSQL rechazaba la
    sentencia. Los parametros se recorren con un unico indice creciente para no
    desalinearlos al concatenar filas.
    """
    import re

    pattern = re.compile(
        r"(INSERT\s+INTO\s+\w+\s*\()\s*([^)]+)\s*(\)\s*VALUES\s*\()", re.IGNORECASE
    )
    match = pattern.search(sql)
    if not match:
        return sql, params

    columns = [c.strip().strip('"').strip("'") for c in match.group(2).split(",")]
    # El grupo 3 del patron ya consumio el `(` de apertura del primer tuple, asi que
    # el scan arranca una posicion antes para incluirlo como fila.
    tuples = _scan_values_tuples(sql, match.end() - 1)
    if not tuples:
        return sql, params

    boolean_indices = {i for i, col in enumerate(columns) if col.lower() in _BOOLEAN_COLUMNS}
    if not boolean_indices:
        return sql, params

    # Todas las filas deben tener la misma aridad que la lista de columnas; si no,
    # no se toca nada (SQL invalido que no nos corresponde interpretar).
    rows = [_split_top_level_items(sql[start + 1 : end]) for start, end in tuples]
    if any(len(row) != len(columns) for row in rows):
        return sql, params

    # Indices globales de parametro que caen en una columna BOOLEAN. El contador
    # avanza por todas las filas y columnas, en el orden en que psycopg3 recibe
    # los parametros.
    boolean_param_positions: list[int] = []
    param_idx = 0
    for row in rows:
        for col_idx, value in enumerate(row):
            if not _is_sql_placeholder(value):
                continue
            if col_idx in boolean_indices:
                boolean_param_positions.append(param_idx)
            param_idx += 1

    if params is not None and boolean_param_positions:
        params_list = list(params)
        for position in boolean_param_positions:
            if position < len(params_list):
                params_list[position] = _to_pg_bool(params_list[position])
        params = tuple(params_list) if isinstance(params, tuple) else params_list

    # Literales: cada fila convierte sus propios 0/1 en las columnas BOOLEAN.
    rendered = []
    for row in rows:
        converted = list(row)
        for col_idx in boolean_indices:
            value = converted[col_idx].strip()
            if value in ("0", "1"):
                converted[col_idx] = "true" if value == "1" else "false"
        rendered.append("(" + ", ".join(converted) + ")")

    return sql[: tuples[0][0]] + ", ".join(rendered) + sql[tuples[-1][1] + 1 :], params


def _adapt_update_boolean_params(sql: str, params: Any = None) -> tuple[str, Any]:
    """Converts integer/boolean params for known BOOLEAN columns in UPDATE SET.

    PostgreSQL rejects integer values (0/1) for BOOLEAN columns, while SQLite
    (and much of the app code) uses 0/1. This function inspects UPDATE...SET
    clauses and converts 0/1/True/False params to Python bool for columns
    known to be BOOLEAN.

    Only modifies params, not SQL (unlike _adapt_boolean_comparisons which
    handles WHERE-clause literals).
    """
    if not params:
        return sql, params

    boolean_columns = _BOOLEAN_COLUMNS

    import re

    # Match patterns like: SET col = ?  or SET col = ?, col2 = ?
    # We need to find which params correspond to boolean columns
    # Pattern: "SET col1 = ?, col2 = ?, col3 = ?"
    params_list = list(params)

    # Find SET clause and extract column->param_index mapping
    set_match = re.search(r"\bSET\s+(.+?)\s*(?:WHERE|$)", sql, re.IGNORECASE | re.DOTALL)
    if not set_match:
        return sql, params

    set_clause = set_match.group(1)
    # Parse assignments: col = ?, col2 = ?, col3 = ?
    # Also handle col = literal_value
    # El conteo de asignaciones se hace sobre el SQL con literales en blanco: un
    # texto como `nota = 'active = 1'` NO es una asignacion booleana, pero si
    # entrara en la lista desplazaria el indice de cada parametro posterior y
    # convertiria el valor equivocado.
    code_clause = _blank_sql_literals(set_clause)
    assignments = re.findall(r'(\w+)\s*=\s*(\?|%s|[\'"]?\d+[\'"]?)', code_clause, re.IGNORECASE)

    # Count params before the SET clause to know the offset
    param_offset = 0
    # Count ? or %s in the part before SET
    before_set = _blank_sql_literals(sql[: set_match.start()])
    param_offset = len(re.findall(r"\?|%s", before_set))

    for idx, (col_name, param_val) in enumerate(assignments):
        if col_name.lower() in boolean_columns and param_val in ("?", "%s"):
            param_idx = param_offset + idx
            if 0 <= param_idx < len(params_list):
                val = params_list[param_idx]
                if isinstance(val, bool):
                    params_list[param_idx] = val
                elif val is True or val == 1:
                    params_list[param_idx] = True
                elif val is False or val == 0:
                    params_list[param_idx] = False

    return sql, tuple(params_list) if isinstance(params, tuple) else params_list


def _adapt_boolean_predicate_params(sql: str, params: Any = None) -> Any:
    """Convierte params 0/1 comparados con columnas BOOLEAN conocidas a bool.

    Psycopg envía enteros como ``int4``; PostgreSQL no define ``boolean = int4``.
    SQLite acepta ese patrón y la capa debe enviar True/False al comparar una
    columna booleana, también en SQL calificado o con varios predicados.
    Literales y comentarios se enmascaran antes de buscar placeholders para que
    no alteren la posición de los parámetros.
    """
    if params is None or isinstance(params, (str, bytes, dict)):
        return params

    import re

    columns = "|".join(re.escape(name) for name in sorted(_BOOLEAN_COLUMNS, key=len, reverse=True))
    placeholder = r"\?|%s"
    comparison = r"(?:=|!=|<>)"
    column = rf"(?:\w+\.)?\b(?:{columns})\b"
    pattern = re.compile(
        rf"(?:{column}\s*{comparison}\s*(?P<right>{placeholder})"
        rf"|(?P<left>{placeholder})\s*{comparison}\s*{column})",
        re.IGNORECASE,
    )
    code = _blank_sql_literals(sql)
    params_list = list(params)
    positions: set[int] = set()
    for match in pattern.finditer(code):
        param_start = (
            match.start("right") if match.group("right") is not None else match.start("left")
        )
        positions.add(len(re.findall(placeholder, code[:param_start])))

    for position in positions:
        if position < len(params_list):
            params_list[position] = _to_pg_bool(params_list[position])

    return tuple(params_list) if isinstance(params, tuple) else params_list


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
        if not getattr(self._conn, "autocommit", False):
            status = _pg_transaction_state(self._conn)
            if _PgTransactionStatus is not None and status in (
                _PgTransactionStatus.INTRANS,
                _PgTransactionStatus.ACTIVE,
                _PgTransactionStatus.INERROR,
            ):
                try:
                    self._conn.rollback()
                except Exception:
                    pass
            elif _pg_is_in_transaction(self._conn):
                try:
                    self._conn.rollback()
                except Exception:
                    pass
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
