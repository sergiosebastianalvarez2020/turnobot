"""Crea una copia consistente de la base SQLite."""

import shutil
from datetime import datetime
from pathlib import Path

from database.database import DATABASE_PATH


def backup_database(destination=None):
    source = Path(DATABASE_PATH)
    if not source.exists():
        raise FileNotFoundError(f"No existe la base de datos: {source}")
    target_dir = Path(destination) if destination else source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"appointments-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(source, target)
    return target


if __name__ == "__main__":
    print(backup_database())
