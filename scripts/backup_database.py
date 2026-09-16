"""Crea una copia consistente de la base SQLite.

Con SQLite en modo WAL, una copia física del archivo .db (shutil.copy2)
puede perder datos recientes que residen en el archivo .db-wal. En su lugar
se utiliza la API oficial de SQLite: sqlite3.Connection.backup(), que
realiza una copia en línea y consistente incluyendo los datos del WAL.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from database import database


def backup_database(destination=None):
    source = Path(database.DATABASE_PATH)
    if not source.exists():
        raise FileNotFoundError(f"No existe la base de datos: {source}")
    target_dir = Path(destination) if destination else source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"appointments-{datetime.now():%Y%m%d-%H%M%S}.db"

    source_conn = sqlite3.connect(str(source), timeout=10)
    try:
        dest_conn = sqlite3.connect(str(target), timeout=10)
        try:
            source_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    return target


if __name__ == "__main__":
    print(backup_database())
