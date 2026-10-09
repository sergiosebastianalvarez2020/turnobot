"""CLI EXPLÍCITO para migraciones de esquema PostgreSQL.

Flujo operativo recomendado:

    backup -> python scripts/migrate_pg.py --dry-run   (revisar)
           -> python scripts/migrate_pg.py             (aplicar)
           -> verificar -> arrancar aplicación

NO se conecta a producción salvo que se le indique la URL correcta; no toca la
suite SQLite ni ``migrations/``. Nunca imprime la contraseña de la conexión.

Uso:
    python scripts/migrate_pg.py [--url DATABASE_URL] [--dry-run] [--migrations-dir DIR]

Exit codes:
    0  éxito (aplicado, adoptado baseline, o nada pendiente);
    1  error controlado (URL inválida, hueco, checksum, esquema parcial, fallo de DDL).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Asegurar que desde cron/CLI pueda importar el paquete del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.pg_migrator import MIGRATIONS_PG_DIR, MigrationError, MigrationResult, run_migrations
from database.pg_pool import get_database_backend, sanitize_database_url

logger = logging.getLogger("turnobot.pg_migrator.cli")


def _format_report(result: MigrationResult) -> str:
    lines = ["Migraciones PostgreSQL"]
    if result.dry_run:
        lines.append("Modo: DRY-RUN (sin escrituras)")
    if result.adopted:
        versions = ", ".join(f"{item.version} ({item.name})" for item in result.adopted)
        lines.append(f"Baseline adoptado (sin ejecutar): {versions}")
    if result.applied:
        versions = ", ".join(f"{item.version} ({item.name})" for item in result.applied)
        lines.append(f"Aplicadas: {versions}")
    if result.pending:
        versions = ", ".join(f"{item.version} ({item.name})" for item in result.pending)
        lines.append(f"Pendientes: {versions}")
    if not result.changed and not result.pending:
        lines.append("Sin migraciones pendientes.")
    lines.append(f"Versión de esquema resultante: {result.current_version}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runner de migraciones PostgreSQL")
    parser.add_argument(
        "--url",
        default=os.environ.get("DATABASE_URL"),
        help="URL/DSN PostgreSQL (por defecto DATABASE_URL del entorno)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Calcula y reporta el plan sin escribir nada"
    )
    parser.add_argument(
        "--migrations-dir",
        default=str(MIGRATIONS_PG_DIR),
        help="Directorio de migraciones .sql (por defecto migrations_pg/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    if not args.url:
        print("ERROR: DATABASE_URL no definida; use --url.", file=sys.stderr)
        return 1

    target = sanitize_database_url(args.url)
    try:
        backend = get_database_backend(args.url)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if backend != "postgresql":
        print("ERROR: el runner solo aplica a PostgreSQL.", file=sys.stderr)
        return 1

    print(f"Destino: {target}")
    try:
        result = run_migrations(args.url, migrations_dir=args.migrations_dir, dry_run=args.dry_run)
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - reportar fallo de DDL/conexión sin traza cruda
        logger.error("Fallo aplicando migraciones: %s", error)
        return 1

    print(_format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
