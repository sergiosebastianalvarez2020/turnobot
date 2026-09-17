"""Verifica la restaurabilidad e integridad del backup automático más reciente.

Trabaja SIEMPRE sobre una copia temporal del backup: nunca abre la base viva
en modo escritura, nunca ejecuta ``restore_database.py`` y nunca toca
``appointments.db`` ni sus sidecars WAL/SHM.

Comprueba sobre la copia:
    * ``PRAGMA integrity_check`` == ok
    * ``PRAGMA foreign_key_check`` sin violaciones
    * ``schema_version`` idéntico al de la base viva
    * conteos de tablas relevantes: la copia es un snapshot anterior, así que
      no puede tener MÁS filas que la base viva; las tablas estables deben
      coincidir exactamente.

Devuelve exit 0 solo si todas las verificaciones pasan.
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUPS_DIR = PROJECT_ROOT / "database" / "backups"
DEFAULT_LIVE_DB = PROJECT_ROOT / "database" / "appointments.db"

BACKUP_NAME_RE = re.compile(r"^appointments-(\d{8})-(\d{6})\.db$")

TABLE_POLICIES = {
    "businesses": "exact",
    "business_settings": "exact",
    "appointments": "monotonic",
}


def parse_timestamp(name):
    match = BACKUP_NAME_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def find_latest_backup(backups_dir):
    """Devuelve la ruta del backup automático más reciente, o None."""
    backups_dir = Path(backups_dir)
    best = None
    with os.scandir(backups_dir) as entries:
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            timestamp = parse_timestamp(entry.name)
            if timestamp is None:
                continue
            if best is None or timestamp > best[0]:
                best = (timestamp, entry.path)
    return best[1] if best else None


def _connect_readonly(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def verify_backup(backup_path, live_db_path):
    """Verifica ``backup_path`` contra ``live_db_path`` (solo lectura).

    Devuelve ``(ok, checks)`` donde ``checks`` es una lista de tuplas
    ``(nombre, aprobado, detalle)``.
    """
    checks = []

    try:
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
            checks.append(("integrity_check", integrity == "ok", str(integrity)))

            fk_rows = backup_conn.execute("PRAGMA foreign_key_check").fetchall()
            checks.append(
                ("foreign_key_check", len(fk_rows) == 0, f"{len(fk_rows)} violaciones")
            )

            backup_schema = backup_conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]
            backup_counts = {
                table: backup_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLE_POLICIES
            }
        finally:
            backup_conn.close()
    except sqlite3.Error as error:
        return False, checks + [("backup_readable", False, str(error))]

    try:
        live_conn = _connect_readonly(live_db_path)
        try:
            live_schema = live_conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]
            live_counts = {
                table: live_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLE_POLICIES
            }
        finally:
            live_conn.close()
    except sqlite3.Error as error:
        return False, checks + [("live_readable", False, str(error))]

    checks.append(
        (
            "schema_version",
            backup_schema == live_schema,
            f"backup={backup_schema} live={live_schema}",
        )
    )

    for table, policy in TABLE_POLICIES.items():
        backup_count = backup_counts[table]
        live_count = live_counts[table]
        if policy == "exact":
            passed = backup_count == live_count
        else:
            passed = backup_count <= live_count
        checks.append(
            (f"count:{table}", passed, f"backup={backup_count} live={live_count} ({policy})")
        )

    return all(check[1] for check in checks), checks


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verifica el backup automático más reciente (read-only)."
    )
    parser.add_argument(
        "--backups-dir",
        default=str(DEFAULT_BACKUPS_DIR),
        help="Directorio de backups (por defecto database/backups).",
    )
    parser.add_argument(
        "--live-db",
        default=str(DEFAULT_LIVE_DB),
        help="Base viva a comparar, en modo lectura.",
    )
    args = parser.parse_args(argv)

    if not Path(args.backups_dir).is_dir():
        print(f"ERROR: no existe el directorio de backups: {args.backups_dir}", file=sys.stderr)
        return 3

    latest = find_latest_backup(args.backups_dir)
    if latest is None:
        print("ERROR: no hay backups automáticos para verificar.", file=sys.stderr)
        return 3

    print(f"Backup más reciente: {latest}")
    print(f"Base viva (read-only): {args.live_db}")

    temp_dir = tempfile.mkdtemp(prefix="turnobot-verify-")
    try:
        copy_path = Path(temp_dir) / Path(latest).name
        shutil.copy2(latest, copy_path)
        ok, checks = verify_backup(copy_path, args.live_db)
        for name, passed, detail in checks:
            print(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
        print("RESULTADO:", "OK" if ok else "FALLO")
        return 0 if ok else 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
