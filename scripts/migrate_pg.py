"""CLI EXPLÍCITO para migraciones de esquema PostgreSQL.

Flujo operativo recomendado:

    backup -> python scripts/migrate_pg.py --dry-run   (revisar)
           -> python scripts/migrate_pg.py             (aplicar)
           -> verificar -> arrancar aplicación

NO se conecta a producción salvo que se le indique la URL correcta; no toca la
suite SQLite ni ``migrations/``. Nunca imprime la contraseña de la conexión.

Carga ``.env`` igual que el resto de los scripts operativos (D8), de modo que la
URL por defecto se resuelve desde el entorno del proceso o desde el archivo
``.env`` del repositorio antes de leerla.

Uso:
    python scripts/migrate_pg.py [--url DATABASE_URL] [--dry-run] [--migrations-dir DIR]
                                 [--lock-timeout SEGUNDOS]

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

from dotenv import load_dotenv

# Igual que app.py y los workers CLI: cargar .env antes de leer la configuración
# de conexión. `override=False` conserva la precedencia del entorno existente.
load_dotenv()

from database.pg_migrator import MIGRATIONS_PG_DIR, MigrationError, MigrationResult, run_migrations
from database.pg_pool import get_database_backend, sanitize_database_url

logger = logging.getLogger("turnobot.pg_migrator.cli")


def _safe_error(error: BaseException) -> str:
    """Representación de un error sin credenciales (defensa en profundidad).

    Las excepciones de la capa de conexión (psycopg) pueden incluir la URL o el
    DSN original en su mensaje; se enmascaran antes de emitirlas a stdout, stderr
    o al logger.
    """
    return sanitize_database_url(str(error)) or ""


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


def _positive_float(value: str) -> float:
    """Tipo argparse: número de segundos estrictamente positivo (D7)."""
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"se esperaba un número en segundos, no {value!r}"
        ) from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"debe ser mayor que 0 segundos, no {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runner de migraciones PostgreSQL")
    parser.add_argument(
        "--url",
        default=os.environ.get("DATABASE_URL"),
        help="URL/DSN PostgreSQL (por defecto DATABASE_URL del entorno)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=_positive_float,
        default=None,
        metavar="SEGUNDOS",
        help=(
            "Espera máxima para adquirir el advisory lock de esquema, en segundos "
            "(por defecto la del migrador: 60 s)"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Calcula y reporta el plan sin escribir nada"
    )
    parser.add_argument(
        "--migrations-dir",
        default=str(MIGRATIONS_PG_DIR),
        help="Directorio de migraciones .sql (por defecto migrations_pg/)",
    )
    parser.add_argument(
        "--allow-external-migrations-dir",
        action="store_true",
        help="Permite usar un directorio de migraciones fuera del repositorio (uso en tests/desarrollo)",
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
        print(f"ERROR: {_safe_error(error)}", file=sys.stderr)
        return 1
    if backend != "postgresql":
        print("ERROR: el runner solo aplica a PostgreSQL.", file=sys.stderr)
        return 1

    print(f"Destino: {target}")
    try:
        result = run_migrations(
            args.url,
            migrations_dir=args.migrations_dir,
            dry_run=args.dry_run,
            lock_timeout=args.lock_timeout,
            allow_external=args.allow_external_migrations_dir,
        )
    except MigrationError as error:
        print(f"ERROR: {_safe_error(error)}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - reportar fallo de DDL/conexión sin traza cruda
        logger.error("Fallo aplicando migraciones: %s", _safe_error(error))
        return 1

    print(_format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
