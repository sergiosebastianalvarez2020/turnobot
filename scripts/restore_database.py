"""Restaura una copia SQLite después de detener la aplicación."""

import shutil
import sys
from pathlib import Path

from database.database import DATABASE_PATH


if len(sys.argv) != 2:
    raise SystemExit("Uso: python scripts/restore_database.py RUTA_BACKUP")

backup = Path(sys.argv[1]).resolve()
if not backup.is_file():
    raise SystemExit(f"Backup no encontrado: {backup}")

DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(backup, DATABASE_PATH)
print(f"Base restaurada desde {backup}")
