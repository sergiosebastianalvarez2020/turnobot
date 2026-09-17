"""Retención segura de backups automáticos de SQLite.

Aplica una política conservadora SOLO sobre archivos cuyo nombre coincide
exactamente con el patrón de backup automático
``appointments-YYYYMMDD-HHMMSS.db``:

    * 7 backups diarios (el más reciente de cada uno de los últimos 7 días)
    * 4 backups semanales (el más reciente de cada una de las últimas 4 semanas ISO)
    * 6 backups mensuales (el más reciente de cada uno de los últimos 6 meses)

Cualquier archivo que NO coincida con el patrón (backups manuales,
pre-migración, sidecars WAL, etc.) se considera PRESERVED y nunca se borra.

Por defecto corre en modo ``--dry-run`` (no elimina nada). Con ``--apply``
elimina únicamente los archivos clasificados como DELETE.

Guardas de seguridad:
    * opera exclusivamente dentro del directorio de backups;
    * no sigue symlinks ni atraviesa subdirectorios;
    * nunca borra el backup más reciente;
    * nunca borra backups del día en curso;
    * nunca borra archivos fuera del patrón automático.
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUPS_DIR = PROJECT_ROOT / "database" / "backups"

BACKUP_NAME_RE = re.compile(r"^appointments-(\d{8})-(\d{6})\.db$")

DAILY_KEEP = 7
WEEKLY_KEEP = 4
MONTHLY_KEEP = 6

MAX_DELETIONS = 1000


def parse_timestamp(name):
    """Devuelve el datetime codificado en el nombre, o None si no matchea."""
    match = BACKUP_NAME_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def list_entries(backups_dir):
    """Clasifica el contenido de ``backups_dir`` sin seguir symlinks.

    Devuelve ``(managed, preserved)`` donde ``managed`` es una lista de
    ``(timestamp, nombre)`` ordenada de más reciente a más antiguo, y
    ``preserved`` es la lista (ordenada) de nombres no gestionados.
    """
    backups_dir = Path(backups_dir)
    managed = []
    preserved = []
    with os.scandir(backups_dir) as entries:
        for entry in entries:
            try:
                is_symlink = entry.is_symlink()
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_symlink or not is_file:
                preserved.append(entry.name)
                continue
            timestamp = parse_timestamp(entry.name)
            if timestamp is None:
                preserved.append(entry.name)
            else:
                managed.append((timestamp, entry.name))
    managed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    preserved.sort()
    return managed, preserved


def plan(
    backups_dir,
    now=None,
    daily_keep=DAILY_KEEP,
    weekly_keep=WEEKLY_KEEP,
    monthly_keep=MONTHLY_KEEP,
):
    """Calcula el plan (keep, delete, preserved) sin tocar el disco."""
    backups_dir = Path(backups_dir)
    now = now or datetime.now()
    managed, preserved = list_entries(backups_dir)

    keep = set()

    if managed:
        keep.add(managed[0][1])

    today = now.date()
    for timestamp, name in managed:
        if timestamp.date() == today:
            keep.add(name)

    def keep_latest_per_bucket(keyfunc, limit):
        if limit <= 0:
            return
        buckets = {}
        for timestamp, name in managed:
            key = keyfunc(timestamp)
            if key not in buckets or (timestamp, name) > buckets[key]:
                buckets[key] = (timestamp, name)
        for key in sorted(buckets.keys(), reverse=True)[:limit]:
            keep.add(buckets[key][1])

    keep_latest_per_bucket(lambda ts: ts.date(), daily_keep)
    keep_latest_per_bucket(lambda ts: ts.isocalendar()[:2], weekly_keep)
    keep_latest_per_bucket(lambda ts: (ts.year, ts.month), monthly_keep)

    return {
        "keep": [name for _, name in managed if name in keep],
        "delete": [name for _, name in managed if name not in keep],
        "preserved": preserved,
    }


def apply_plan(backups_dir, plan_result):
    """Elimina los archivos de ``plan_result['delete']`` con guardas estrictas."""
    backups_dir = Path(backups_dir).resolve()
    deleted = []
    for name in plan_result["delete"]:
        if not BACKUP_NAME_RE.match(name):
            raise RuntimeError(f"Guardia: nombre no gestionado en DELETE: {name}")
        target = backups_dir / name
        if target.resolve().parent != backups_dir:
            raise RuntimeError(f"Guardia: ruta fuera del directorio de backups: {target}")
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"Guardia: no es un archivo regular: {target}")
        os.remove(target)
        deleted.append(name)
    return deleted


def format_plan(plan_result, mode):
    lines = [f"Modo: {mode}"]
    for name in plan_result["keep"]:
        lines.append(f"KEEP      {name}")
    for name in plan_result["delete"]:
        lines.append(f"DELETE    {name}")
    for name in plan_result["preserved"]:
        lines.append(f"PRESERVED {name}")
    lines.append(
        f"Resumen: {len(plan_result['keep'])} keep, "
        f"{len(plan_result['delete'])} delete, "
        f"{len(plan_result['preserved'])} preserved"
    )
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Retención de backups automáticos (dry-run por defecto)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Elimina realmente los archivos marcados como DELETE.",
    )
    parser.add_argument(
        "--dir",
        default=str(DEFAULT_BACKUPS_DIR),
        help="Directorio de backups (por defecto database/backups).",
    )
    args = parser.parse_args(argv)

    backups_dir = Path(args.dir)
    if not backups_dir.is_dir():
        print(f"ERROR: no existe el directorio de backups: {backups_dir}", file=sys.stderr)
        return 2

    plan_result = plan(backups_dir)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Directorio: {backups_dir}")
    for line in format_plan(plan_result, mode):
        print(line)

    if not args.apply:
        print("DRY-RUN: no se eliminó ningún archivo.")
        return 0

    if len(plan_result["delete"]) > MAX_DELETIONS:
        print(
            "ABORTADO: el plan de borrado excede el máximo de seguridad "
            f"({MAX_DELETIONS}).",
            file=sys.stderr,
        )
        return 3

    deleted = apply_plan(backups_dir, plan_result)
    for name in deleted:
        print(f"ELIMINADO {name}")
    print(f"Total eliminados: {len(deleted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
