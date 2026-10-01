"""Restore de PostgreSQL usando pg_restore.

El backup generado por scripts/backup_postgresql.py se restaura con una base
Aislada mediante `pg_restore`, evitando reponer la base de producción y usando
variables libpq para evitar que la contraseña quede expuesta en la línea de
comandos.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

from database.pg_pool import normalize_database_url


def build_pg_env(url: str) -> dict[str, str]:
    """Construye el entorno libpq con PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD."""
    normalized = normalize_database_url(url)
    if not normalized or not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL no es una URL PostgreSQL válida")

    from urllib.parse import urlparse

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
    source_env = build_pg_env(url)
    admin_env = source_env.copy()
    admin_env["PGDATABASE"] = "postgres"

    if target_dbname is None:
        target_dbname = f"turnobot_restore_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    subprocess.run(["dropdb", "--if-exists", target_dbname], env=admin_env, check=False)
    subprocess.run(["createdb", target_dbname], env=admin_env, check=True)

    restore_env = source_env.copy()
    restore_env["PGDATABASE"] = target_dbname
    cmd = build_pg_restore_command(backup, target_dbname)
    result = subprocess.run(
        cmd,
        env=restore_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "pg_restore falló").strip()
        raise RuntimeError(f"pg_restore falló: {stderr}")

    print(f"Restore completado: base '{target_dbname}'")
    print(f"Backup: {backup}")
    return target_dbname


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/restore_postgresql.py RUTA_BACKUP [DBNAME]")
        raise SystemExit(1)

    backup = sys.argv[1]
    dbname = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        restore_database(backup, dbname)
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
