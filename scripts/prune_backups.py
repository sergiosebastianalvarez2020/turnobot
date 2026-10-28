"""Retención segura de backups automáticos (SQLite y PostgreSQL).

Aplica una política conservadora SOLO sobre archivos cuyo nombre coincide
exactamente con uno de los patrones de backup automático:

    * ``appointments-YYYYMMDD-HHMMSS.db``   (SQLite)
    * ``turnobot-pg-YYYYMMDD-HHMMSS.dump``  (PostgreSQL, pg_dump -Fc)

    * 7 backups diarios (el más reciente de cada uno de los últimos 7 días)
    * 4 backups semanales (el más reciente de cada una de las últimas 4 semanas ISO)
    * 6 backups mensuales (el más reciente de cada uno de los últimos 6 meses)

La retención se calcula POR FORMATO: cada patrón agrupa sus propios cubos
diario/semanal/mensual, de modo que un volcado PostgreSQL nunca desplaza a un
backup SQLite del mismo directorio (ni a la inversa). Con un solo formato en
el directorio el resultado es idéntico al comportamiento histórico SQLite.

Cualquier archivo que NO coincida con los patrones (backups manuales,
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

BACKUP_NAME_RES = (
    ("sqlite", re.compile(r"^appointments-(\d{8})-(\d{6})\.db$")),
    ("postgres", re.compile(r"^turnobot-pg-(\d{8})-(\d{6})\.dump$")),
)

DAILY_KEEP = 7
WEEKLY_KEEP = 4
MONTHLY_KEEP = 6

MAX_DELETIONS = 1000


def match_managed(name):
    """Devuelve ``(formato, timestamp)`` si ``name`` es un backup automático.

    ``formato`` es la clave de agrupación de la retención (``sqlite`` o
    ``postgres``). Devuelve ``None`` cuando el nombre no coincide con ningún
    patrón o cuando la fecha codificada no es válida: en ambos casos el
    archivo queda PRESERVED y jamás se borra.
    """
    for kind, pattern in BACKUP_NAME_RES:
        match = pattern.match(name)
        if not match:
            continue
        try:
            timestamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return kind, timestamp
    return None


def parse_timestamp(name):
    """Devuelve el datetime codificado en el nombre, o None si no matchea."""
    matched = match_managed(name)
    return matched[1] if matched else None


def list_entries(backups_dir):
    """Clasifica el contenido de ``backups_dir`` sin seguir symlinks.

    Devuelve ``(managed, preserved)`` donde ``managed`` es una lista de
    ``(formato, timestamp, nombre)`` ordenada de más reciente a más antiguo, y
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
            matched = match_managed(entry.name)
            if matched is None:
                preserved.append(entry.name)
            else:
                managed.append((matched[0], matched[1], entry.name))
    managed.sort(key=lambda item: (item[1], item[2]), reverse=True)
    preserved.sort()
    return managed, preserved


def plan(
    backups_dir, now=None, daily_keep=DAILY_KEEP, weekly_keep=WEEKLY_KEEP, monthly_keep=MONTHLY_KEEP
):
    """Calcula el plan (keep, delete, preserved) sin tocar el disco."""
    backups_dir = Path(backups_dir)
    now = now or datetime.now()
    managed, preserved = list_entries(backups_dir)

    keep = set()

    if managed:
        keep.add(managed[0][2])

    today = now.date()
    for _, timestamp, name in managed:
        if timestamp.date() == today:
            keep.add(name)

    def keep_latest_per_bucket(keyfunc, limit):
        if limit <= 0:
            return
        buckets = {}
        for kind, timestamp, name in managed:
            key = keyfunc(kind, timestamp)
            if key not in buckets or (timestamp, name) > buckets[key]:
                buckets[key] = (timestamp, name)
        for kind in {entry[0] for entry in managed}:
            kind_keys = sorted((key for key in buckets if key[0] == kind), reverse=True)[:limit]
            for key in kind_keys:
                keep.add(buckets[key][1])

    keep_latest_per_bucket(lambda kind, ts: (kind, ts.date()), daily_keep)
    keep_latest_per_bucket(lambda kind, ts: (kind, ts.isocalendar()[:2]), weekly_keep)
    keep_latest_per_bucket(lambda kind, ts: (kind, (ts.year, ts.month)), monthly_keep)

    return {
        "keep": [name for _, _, name in managed if name in keep],
        "delete": [name for _, _, name in managed if name not in keep],
        "preserved": preserved,
    }


def apply_plan(backups_dir, plan_result):
    """Elimina los archivos de ``plan_result['delete']`` con guardas estrictas."""
    backups_dir = Path(backups_dir).resolve()
    deleted = []
    for name in plan_result["delete"]:
        if match_managed(name) is None:
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
        "--apply", action="store_true", help="Elimina realmente los archivos marcados como DELETE."
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
            f"ABORTADO: el plan de borrado excede el máximo de seguridad ({MAX_DELETIONS}).",
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
