"""Módulo de infraestructura y ciclo de vida del Pool de Conexiones PostgreSQL (Fase 4C - 4F).

Proporciona abstracción mínima para la inicialización, acceso y cierre
del pool de conexiones psycopg_pool.ConnectionPool, así como proxies de conexión,
cursor y filas (PgRowProxy, PgCursorProxy, PgConnectionProxy) para adaptar la sintaxis
y acceso de datos de SQLite a PostgreSQL sin alterar la funcionalidad sobre SQLite.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
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
    """Inicializa el pool de conexiones PostgreSQL para la aplicación Flask o a nivel global."""
    global _global_pool

    effective_url = conninfo
    if effective_url is None and app is not None:
        effective_url = app.config.get("DATABASE_URL")

    backend = get_database_backend(effective_url)
    if backend != "postgresql" or not effective_url:
        logger.debug("Backend actual es %s; no se inicializa pool de PostgreSQL", backend)
        return None

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
    else:
        _global_pool = pool

    return pool


def get_pg_pool(app: Flask | None = None) -> psycopg_pool.ConnectionPool | None:
    """Obtiene la instancia de ConnectionPool activa desde app context o global."""
    if app is not None and hasattr(app, "extensions") and "pg_pool" in app.extensions:
        return app.extensions["pg_pool"]
    return _global_pool


def close_pg_pool(
    pool: psycopg_pool.ConnectionPool | None = None, app: Flask | None = None, timeout: float = 5.0
) -> None:
    """Cierra limpiamente un pool de conexiones PostgreSQL y limpia referencias."""
    global _global_pool

    target_pool = pool
    if target_pool is None and app is not None:
        if hasattr(app, "extensions") and "pg_pool" in app.extensions:
            target_pool = app.extensions.pop("pg_pool")

    if target_pool is None:
        target_pool = _global_pool

    if target_pool is not None:
        if target_pool is _global_pool:
            _global_pool = None
        try:
            logger.info("Cerrando pool PostgreSQL")
            target_pool.close(timeout=timeout)
        except Exception as err:
            logger.warning("Error al cerrar pool PostgreSQL: %s", err)


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

    # 6. Reemplazar placeholders '?' por '%s' fuera de comillas
    sql = replace_placeholders(sql)

    # 7. Escapar '%' literales para el parser de psycopg3
    sql = escape_literal_percent(sql)

    return sql, params


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
            connection.begin()
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
            _call_translated(lambda: self._cursor.execute(adapted_script))
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
