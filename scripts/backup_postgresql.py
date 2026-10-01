"""Backup PostgreSQL mediante pg_dump.

Usa el cliente nativo de PostgreSQL para producir un archivo custom, comprimido y
restaurable con pg_restore. Se evita incluir credenciales en la línea de comandos
porque la contraseña se inyecta en el entorno del proceso mediante variables
libpq (PGPASSWORD, PGHOST, PGPORT, PGDATABASE, PGUSER).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from database.pg_pool import normalize_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUPS_DIR = PROJECT_ROOT / "backups_pg"
MIN_BACKUP_BYTES = 1024


def build_pg_env(url: str) -> dict[str, str]:
    """Construye el entorno libpq necesario para ejecutar pg_dump/pg_restore."""
    normalized = normalize_database_url(url)
    if not normalized or not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL no es una URL PostgreSQL válida")

    parsed = urlparse(normalized)
    username = parsed.username or "turnobot"
    password = parsed.password or ""
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    database = (parsed.path or "/").lstrip("/") or "postgres"

    env = os.environ.copy()
    env["PGHOST"] = host
    env["PGPORT"] = port
    env["PGDATABASE"] = database
    env["PGUSER"] = username
    if password:
        env["PGPASSWORD"] = password
    return env


def sanitize_pg_env(env: dict[str, str]) -> dict[str, str]:
    """Mascara los secretos antes de mostrarlos en logs o errores."""
    sanitized = dict(env)
    if "PGPASSWORD" in sanitized:
        sanitized["PGPASSWORD"] = "***"
    return sanitized


def get_database_url() -> str:
    """Devuelve la URL PostgreSQL normalizada desde el entorno."""
    url = os.environ.get("DATABASE_URL") or os.environ.get("TURNOBOT_PG_URL")
    if not url:
        raise ValueError("DATABASE_URL o TURNOBOT_PG_URL debe estar configurada")
    normalized = normalize_database_url(url)
    if not normalized or not normalized.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL no es una URL PostgreSQL válida")
    return normalized


def build_pg_dump_command(url: str, destination: Path | str) -> list[str]:
    """Construye el comando de pg_dump con formato custom y compresión."""
    parsed = urlparse(normalize_database_url(url))
    username = parsed.username or "turnobot"
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    database = (parsed.path or "/").lstrip("/") or "postgres"
    dsn_without_password = f"postgresql://{username}@{host}:{port}/{database}"
    destination_path = str(Path(destination)).replace("\\", "/")
    return [
        "pg_dump",
        "--format=c",
        "--compress=9",
        f"--dbname={dsn_without_password}",
        f"--file={destination_path}",
    ]


def backup_database(destination: str | Path | None = None) -> Path:
    """Crea un backup PostgreSQL valido con pg_dump y comprime el artefacto."""
    url = get_database_url()
    backup_dir = Path(destination) if destination else DEFAULT_BACKUPS_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which("pg_dump") is None:
        raise FileNotFoundError("pg_dump no está disponible en PATH")

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"turnobot-pg-{timestamp}.dump"
    env = build_pg_env(url)
    cmd = build_pg_dump_command(url, target)

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "pg_dump falló").strip()
        raise RuntimeError(f"pg_dump falló: {stderr}")

    if not target.exists():
        raise FileNotFoundError(f"No se generó el backup: {target}")

    file_size = target.stat().st_size
    if file_size < MIN_BACKUP_BYTES:
        raise RuntimeError(
            f"El backup parece demasiado pequeño para ser válido ({file_size} bytes): {target}"
        )

    print(f"Backup creado: {target}")
    print(f"Tamaño: {file_size} bytes ({file_size / 1024:.1f} KB)")
    print(f"Base: {Path(urlparse(url).path).name or 'postgres'}")
    print("Formato: custom (pg_dump -Fc)")
    return target


if __name__ == "__main__":
    try:
        backup_database()
    except Exception as exc:  # pragma: no cover - CLI failure path
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
