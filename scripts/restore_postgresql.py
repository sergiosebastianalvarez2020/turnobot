"""Restore de PostgreSQL usando pg_restore.

El backup generado por scripts/backup_postgresql.py se restaura con una base
Aislada mediante `pg_restore`, evitando reponer la base de producción y usando
variables libpq para evitar que la contraseña quede expuesta en la línea de
comandos.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from database.pg_pool import normalize_database_url

# Nombres de base reservados del sistema: destruirlos deja el servidor
# inoperable o sin base administrativa. Se rechazan siempre (fail-safe).
RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})

# Identificador PostgreSQL válido: debe citarse sin comillas en dropdb/
# createdb. Cualquier otro valor (opciones CLI como "--host=...", cadenas
# con espacios, ";", "$", etc.) se rechaza antes de ejecutar subprocess.
_SAFE_DBNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Timeouts para evitar cuelgues indefinidos (p. ej. conexiones colgadas).
DROPDB_TIMEOUT_SECONDS = 120
CREATEDB_TIMEOUT_SECONDS = 120
PG_RESTORE_TIMEOUT_SECONDS = 600


def build_pg_env(url: str) -> dict[str, str]:
    """Construye el entorno libpq con PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD."""
    normalized = normalize_database_url(url)
    if not normalized or not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL no es una URL PostgreSQL válida")

    parsed = urlparse(normalized)
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    username = parsed.username or "turnobot"
    database = (parsed.path or "/").lstrip("/") or "postgres"
    password = parsed.password or ""

    env = os.environ.copy()
    env["PGHOST"] = host
    env["PGPORT"] = port
    env["PGDATABASE"] = database
    env["PGUSER"] = username
    if password:
        env["PGPASSWORD"] = password
    return env


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("TURNOBOT_PG_URL")
    if not url:
        raise ValueError("DATABASE_URL o TURNOBOT_PG_URL debe estar configurada")
    normalized = normalize_database_url(url)
    if not normalized or not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL no es una URL PostgreSQL válida")
    return normalized


def parse_database_identity(url: str) -> dict[str, str]:
    """Extrae la identidad (host, puerto, base, usuario) de una URL PostgreSQL.

    La identidad de una base como para ``dropdb`` está dada por (host, puerto,
    base): el usuario es solo credencial de acceso y no cambia cuál base se
    destruye. ``host`` se normaliza a minúsculas (insensible a mayúsculas en
    libpq). Se usa para identificar la base productiva y rechazar destinos
    que colisionen con ella.
    """
    normalized = normalize_database_url(url)
    if not normalized or not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL no es una URL PostgreSQL válida")
    parsed = urlparse(normalized)
    return {
        "host": (parsed.hostname or "localhost").lower(),
        "port": str(parsed.port or 5432),
        "database": (parsed.path or "/").lstrip("/") or "postgres",
        "user": parsed.username or "",
    }


def is_dangerous_destination(
    target_dbname: str, target_host: str, target_port: str, production_url: str
) -> bool:
    """Indica si un destino coincide con la base productiva (host, puerto, nombre).

    La comparación usa la identidad completa y no solo el nombre: el mismo
    nombre de base en otro servidor no se considera peligroso, de modo que no
    se bloquean restauraciones legítimas en entornos aislados. Si no se puede
    identificar la base productiva, se considera peligroso (fail-safe).
    """
    try:
        prod = parse_database_identity(production_url)
    except ValueError:
        return True
    return (
        target_host == prod["host"]
        and target_port == prod["port"]
        and target_dbname == prod["database"]
    )


def validate_restore_target(
    target_dbname: str, target_host: str, target_port: str, production_url: str
) -> None:
    """Barrera de seguridad: rechaza destinos peligrosos.

    Debe invocarse ANTES del primer comando destructivo (``dropdb``). Rechaza
    (fail-safe):

    - destinos que coincidan con la base productiva (host, puerto y nombre);
    - bases reservadas del sistema (``postgres``, ``template0``,
      ``template1``), cuyo DROP deja el servidor inoperable;
    - nombres que no sean identificadores PostgreSQL válidos (bloquea
      inyección de opciones CLI como ``--host=...`` u otros metacaracteres).

    Si no se puede identificar la base productiva, rechaza el destino en
    lugar de asumir que es seguro continuar.
    """
    if not target_dbname or target_dbname.lower() in RESERVED_DATABASES:
        raise ValueError(
            f"Barrera de seguridad: el destino '{target_dbname}' es una base "
            "reservada del sistema. No se permite destruirla."
        )
    if not _SAFE_DBNAME_RE.match(target_dbname):
        raise ValueError(
            f"Barrera de seguridad: el destino '{target_dbname}' no es un "
            "identificador PostgreSQL válido. Use solo letras, dígitos y "
            "guion bajo (por ejemplo turnobot_restore_<timestamp>)."
        )
    try:
        prod = parse_database_identity(production_url)
    except ValueError as exc:
        raise ValueError(
            "Barrera de seguridad: no se pudo identificar la base productiva "
            f"({exc}). Se aborta antes de dropdb (fail-safe)."
        ) from exc
    if (
        target_host == prod["host"]
        and target_port == prod["port"]
        and target_dbname == prod["database"]
    ):
        raise ValueError(
            f"Barrera de seguridad: el destino '{target_dbname}' coincide con la "
            f"base productiva ({prod['host']}:{prod['port']}/{prod['database']}). "
            "No se permite restaurar sobre la base productiva. Use un nombre "
            "temporal (por ejemplo turnobot_restore_<timestamp>)."
        )


def build_pg_restore_command(backup_path: str | Path, target_dbname: str) -> list[str]:
    """Construye el comando de pg_restore para una base destino aislada."""
    return [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        f"--dbname={target_dbname}",
        str(Path(backup_path)),
    ]


def restore_database(backup_path: str | Path, target_dbname: str | None = None):
    """Restaura el backup custom en una base temporal de prueba, nunca en producción."""
    backup = Path(backup_path)
    if not backup.exists():
        raise FileNotFoundError(f"Backup no encontrado: {backup}")
    if shutil.which("pg_restore") is None:
        raise FileNotFoundError("pg_restore no está disponible en PATH")

    url = get_database_url()
    prod = parse_database_identity(url)

    if target_dbname is None:
        target_dbname = f"turnobot_restore_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # BARRERA DE SEGURIDAD: validar ANTES del primer comando destructivo (dropdb).
    # El restore se conecta al servidor de DATABASE_URL; un destino que coincida
    # con la base productiva (host, puerto y nombre) se rechaza para impedir
    # un dropdb accidental sobre producción.
    validate_restore_target(target_dbname, prod["host"], prod["port"], url)

    source_env = build_pg_env(url)
    admin_env = source_env.copy()
    admin_env["PGDATABASE"] = "postgres"

    # "--" separa las opciones de los operandos: defensa en profundidad
    # contra nombres que el parser de dropdb/createdb tome como flags.
    # (La validación de identificador previa ya rechaza esos valores.)
    subprocess.run(
        ["dropdb", "--if-exists", "--", target_dbname],
        env=admin_env,
        check=False,
        timeout=DROPDB_TIMEOUT_SECONDS,
    )
    subprocess.run(
        ["createdb", "--", target_dbname],
        env=admin_env,
        check=True,
        timeout=CREATEDB_TIMEOUT_SECONDS,
    )

    restore_env = source_env.copy()
    restore_env["PGDATABASE"] = target_dbname
    cmd = build_pg_restore_command(backup, target_dbname)
    result = subprocess.run(
        cmd,
        env=restore_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=PG_RESTORE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "pg_restore falló").strip()
        raise RuntimeError(f"pg_restore falló: {stderr}")

    print(f"Restore completado: base '{target_dbname}'")
    print(f"Backup: {backup}")
    return target_dbname


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Uso: python scripts/restore_postgresql.py RUTA_BACKUP [DBNAME]\n"
            "\n"
            "Restaura un backup PostgreSQL en una base aislada. Por seguridad se\n"
            "rechaza, antes de dropdb, cualquier destino que coincida con la base\n"
            "productiva definida por DATABASE_URL (host, puerto y nombre). Si omite\n"
            "DBNAME se usa un nombre temporal (turnobot_restore_<timestamp>), siempre\n"
            "seguro."
        )
        raise SystemExit(0)

    if len(sys.argv) < 2:
        print(
            "Uso: python scripts/restore_postgresql.py RUTA_BACKUP [DBNAME]\n"
            "Si omite DBNAME, restaura en una base temporal segura.\n"
            "Se rechaza cualquier destino que coincida con la base productiva\n"
            "(DATABASE_URL). Use --help para más detalle."
        )
        raise SystemExit(1)

    backup = sys.argv[1]
    dbname = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        restore_database(backup, dbname)
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
