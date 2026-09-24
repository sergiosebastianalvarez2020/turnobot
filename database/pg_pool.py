"""Módulo de infraestructura y ciclo de vida del Pool de Conexiones PostgreSQL (Fase 4C).

Proporciona abstracción mínima para la inicialización, acceso y cierre
del pool de conexiones psycopg_pool.ConnectionPool sin alterar la funcionalidad
actual sobre SQLite.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import psycopg_pool

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
    """Inicializa el pool de conexiones PostgreSQL para la aplicación Flask o a nivel global.

    Si conninfo no está definido, intenta obtenerlo de app.config["DATABASE_URL"]
    o de la variable de entorno DATABASE_URL.
    Si el backend detectado no es PostgreSQL, devuelve None.
    """
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
# CONNECTION LIFECYCLE PROXY (Fase 4D.1)
# ============================================================
# TurnoGo asume el contrato sqlite3.Connection: abrir con get_connection()
# y finalizar con connection.close(). En psycopg_pool, getconn()/putconn()
# son asimétricos: connection.close() NO devuelve la conexión al pool (la
# termina en el backend). Este proxy pequeño traduce close() -> putconn()
# de forma idempotente, evitando fuga o agotamiento del pool.
#
# Sólo administra el LIFECYCLE. NO emulate row_factory ni row["col"]; el acceso
# por nombre de columna y los placeholders `?` dependen de la migración SQL
# (FASE 4E), fuera del alcance de 4D.1.


class PgConnectionProxy:
    """Envoltura mínima de psycopg_connection para adapter con el contrato
    sqlite3.Connection usado por TurnoGo (close -> putconn)."""

    __slots__ = ("_conn", "_pool", "_returned")

    def __init__(self, conn: Any, pool: psycopg_pool.ConnectionPool) -> None:
        self._conn = conn
        self._pool = pool
        self._returned = False

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        try:
            self._pool.putconn(self._conn)
        except Exception as err:
            logger.warning("putconn falló, cerrando conexion: %s", err)
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
