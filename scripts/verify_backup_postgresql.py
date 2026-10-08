"""Verifica la integridad del archivo de backup PostgreSQL.

Comprueba que el dump generado por pg_dump es legible por pg_restore y que el
archivo no está vacío. La validación de contenido y constraints se hace sobre una
base de prueba aislada durante la restauración, nunca sobre la base de producción.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUPS_DIR = PROJECT_ROOT / "backups_pg"

# Timeout para que pg_restore --list no cuelgue indefinidamente con un
# archivo colgado o un binario que no responde. La verificación es
# read-only: nunca crea ni elimina bases.
PG_RESTORE_LIST_TIMEOUT_SECONDS = 120


def get_latest_backup(backups_dir: str | Path | None = None) -> Path | None:
    """Devuelve el último backup de PostgreSQL generado por el script."""
    backup_dir = Path(backups_dir) if backups_dir else DEFAULT_BACKUPS_DIR
    if not backup_dir.exists():
        return None

    candidates = sorted(backup_dir.glob("turnobot-pg-*.dump"))
    if not candidates:
        return None
    return candidates[-1]


def build_restore_listing_command(backup_path: str | Path) -> list[str]:
    """Construye la comprobación `pg_restore --list` para validar el formato."""
    return ["pg_restore", "--list", str(Path(backup_path))]


def verify_backup(backup_path: str | Path) -> bool:
    """Valida que el archivo de backup es un dump pg_dump legible."""
    backup = Path(backup_path)
    if not backup.exists():
        raise FileNotFoundError(f"Backup no encontrado: {backup}")

    if shutil.which("pg_restore") is None:
        raise FileNotFoundError("pg_restore no está disponible en PATH")

    if backup.stat().st_size <= 0:
        raise RuntimeError(f"Backup vacío o corrupto: {backup}")

    cmd = build_restore_listing_command(backup)
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=PG_RESTORE_LIST_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "pg_restore --list falló").strip()
        raise RuntimeError(f"Backup inválido: {stderr}")

    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        latest = get_latest_backup()
        if latest is None:
            print("No se encontró ningún backup PostgreSQL en backups_pg/", file=sys.stderr)
            return 2
        backup_path = latest
    elif len(args) == 1:
        backup_path = Path(args[0])
    else:
        print("Uso: python scripts/verify_backup_postgresql.py [RUTA_BACKUP]", file=sys.stderr)
        return 3

    print(f"Backup: {backup_path}")
    try:
        ok = verify_backup(backup_path)
    except Exception as exc:
        print(f"RESULTADO: FAIL -> {exc}", file=sys.stderr)
        return 1

    print("RESULTADO: OK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
