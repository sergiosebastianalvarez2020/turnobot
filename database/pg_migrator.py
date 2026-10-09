"""Runner EXPLÍCITO de migraciones de esquema PostgreSQL.

Este módulo implementa un mecanismo de migración versionada, incremental y
auditable para el esquema PostgreSQL, SEPARADO de ``init_database()``. El
arranque de la aplicación sigue aplicando ``migrations_pg/001_initial_schema.sql``
solo a una base vacía (bootstrap); este runner se ejecuta de forma explícita en
el flujo operativo:

    backup -> ejecutar runner -> verificar -> arrancar aplicación

Contrato:

- Tabla de control ``schema_migrations(version, name, checksum, applied_at)``.
- Orden determinista por versión numérica del nombre de archivo.
- Solo aplica migraciones pendientes (no repite las ya aplicadas).
- Detecta checksum cambiado de una migración ya aplicada y falla.
- Detecta huecos de versión y migraciones desconocidas (downgrade) y falla.
- Advisory lock a nivel de sesión para serializar ejecuciones concurrentes
  (comparte la clave del bootstrap para no competir con el arranque de la app).
  La espera está acotada (por defecto 60 s, configurable): si otro proceso
  retiene el lock más tiempo, falla con ``MigrationLockTimeout`` (no cuelga).
- Cada migración se aplica en su PROPIA transacción: si una falla, se hace
  rollback de esa transacción y NO se registra como aplicada.
- Auto-baseline: si no hay metadata pero el esquema 001 ya está completo, se
  registra la versión 1 SIN ejecutarla y se continúa con las pendientes.

El mecanismo SQLite (``database.database.apply_migrations``) permanece intacto e
independiente: este runner nunca lee ``migrations/``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import time
from pathlib import Path

import psycopg

from database.database import _PG_SCHEMA_BOOTSTRAP_LOCK
from database.pg_pool import normalize_database_url, split_sql_statements

logger = logging.getLogger("turnobot.pg_migrator")

BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_PG_DIR = BASE_DIR / "migrations_pg"

BASELINE_VERSION = 1
METADATA_TABLE = "schema_migrations"

# Comparte la clave del bootstrap de PostgreSQL: serializa a la vez runner vs
# runner y runner vs arranque de la aplicación (bootstrap). Evita que un
# bootstrap y una migración muten el esquema en paralelo.
_PG_SCHEMA_LOCK = _PG_SCHEMA_BOOTSTRAP_LOCK

# Límite de espera del advisory lock antes de fallar con MigrationLockTimeout.
_LOCK_WAIT_TIMEOUT = 60.0
_LOCK_POLL_INTERVAL = 0.25

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9_]+)\.sql$")
_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_]\w*)", re.I)
_CREATE_INDEX_RE = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+([a-zA-Z_]\w*)", re.I)
_TRANSACTION_COMMANDS = frozenset({"BEGIN", "COMMIT", "ROLLBACK"})

_METADATA_DDL = f"""
CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
    version BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """Error controlado del runner (hueco, checksum, esquema parcial, downgrade)."""


class MigrationLockTimeout(MigrationError):
    """No se pudo adquirir el advisory lock dentro del límite de espera."""


@dataclasses.dataclass(frozen=True)
class MigrationFile:
    """Una migración descubierta en disco."""

    version: int
    name: str
    path: Path
    checksum: str


@dataclasses.dataclass
class MigrationPlan:
    """Plan de ejecución calculado bajo el advisory lock."""

    to_adopt: list[MigrationFile]
    to_apply: list[MigrationFile]


@dataclasses.dataclass
class MigrationResult:
    """Resultado de una ejecución (o simulación) del runner."""

    dry_run: bool
    adopted: list[MigrationFile]
    applied: list[MigrationFile]
    pending: list[MigrationFile]
    current_version: int

    @property
    def changed(self) -> bool:
        return bool(self.adopted or self.applied)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover_migrations(migrations_dir: Path | str = MIGRATIONS_PG_DIR) -> list[MigrationFile]:
    """Descubre y valida las migraciones de un directorio.

    Exige versiones únicas y contiguas desde 1 sin huecos. Un hueco o un nombre
    inválido produce ``MigrationError`` antes de tocar la base de datos.
    """
    directory = Path(migrations_dir)
    discovered: list[MigrationFile] = []
    for path in directory.glob("*.sql"):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if match is None:
            continue
        discovered.append(
            MigrationFile(
                version=int(match.group(1)),
                name=path.name,
                path=path,
                checksum=_checksum(path.read_text(encoding="utf-8")),
            )
        )

    discovered.sort(key=lambda item: item.version)
    if not discovered:
        raise MigrationError(f"No se encontraron migraciones .sql en {directory}")

    versions = [item.version for item in discovered]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"Versiones de migración duplicadas: {versions}")
    if versions != list(range(BASELINE_VERSION, len(versions) + 1)):
        raise MigrationError(
            "Huecos o versiones no contiguas en las migraciones: "
            f"{versions}; se esperaba {list(range(BASELINE_VERSION, len(versions) + 1))}"
        )
    return discovered


def _metadata_exists(conn) -> bool:
    row = conn.execute(f"SELECT to_regclass('public.{METADATA_TABLE}')").fetchone()
    return bool(row and row[0])


def _ensure_metadata_table(conn) -> None:
    conn.execute(_METADATA_DDL)
    conn.commit()


def _load_applied(conn) -> dict[int, dict]:
    """Carga las migraciones registradas como ``{version: {name, checksum}}``."""
    if not _metadata_exists(conn):
        return {}
    rows = conn.execute(
        f"SELECT version, name, checksum FROM {METADATA_TABLE} ORDER BY version"
    ).fetchall()
    return {int(row[0]): {"name": row[1], "checksum": row[2]} for row in rows}


def _current_version(conn) -> int:
    if not _metadata_exists(conn):
        return 0
    row = conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM {METADATA_TABLE}").fetchone()
    return int(row[0]) if row else 0


def _database_objects(conn) -> tuple[set[str], set[str]]:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ).fetchall()
    }
    return tables, indexes


def _expected_objects(sql: str) -> tuple[set[str], set[str]]:
    """Tablas e índices que declara un script de migración (misma detección que el bootstrap)."""
    tables = set(_CREATE_TABLE_RE.findall(sql))
    indexes = set(_CREATE_INDEX_RE.findall(sql))
    return tables, indexes


def _classify_against_baseline(conn, baseline: MigrationFile) -> str:
    """Clasifica la base frente al baseline: ``fresh``, ``partial`` o ``complete``."""
    expected_tables, expected_indexes = _expected_objects(baseline.path.read_text(encoding="utf-8"))
    existing_tables, existing_indexes = _database_objects(conn)

    present = (expected_tables & existing_tables) or (expected_indexes & existing_indexes)
    if not present:
        return "fresh"

    missing_tables = expected_tables - existing_tables
    missing_indexes = expected_indexes - existing_indexes
    if missing_tables or missing_indexes:
        return "partial"
    return "complete"


def _verify_applied(migrations: list[MigrationFile], applied: dict[int, dict]) -> None:
    """Valida registro aplicado contra los archivos: contigüidad, downgrade y checksum."""
    if not applied:
        return

    by_version = {item.version: item for item in migrations}
    applied_versions = sorted(applied)

    if applied_versions != list(range(BASELINE_VERSION, len(applied_versions) + 1)):
        raise MigrationError(
            f"Registro de migraciones no contiguo desde {BASELINE_VERSION}: {applied_versions}"
        )

    unknown = [version for version in applied_versions if version not in by_version]
    if unknown:
        raise MigrationError(
            "La base tiene migraciones que el código no conoce (¿código más antiguo "
            f"que el esquema?): {unknown}"
        )

    for version in applied_versions:
        expected = by_version[version].checksum
        recorded = applied[version]["checksum"]
        if recorded != expected:
            raise MigrationError(
                f"Checksum modificado en la migración {version} "
                f"({by_version[version].name}): registrado {recorded[:12]}… != "
                f"archivo {expected[:12]}…"
            )


def plan_migrations(conn, migrations_dir: Path | str = MIGRATIONS_PG_DIR) -> MigrationPlan:
    """Calcula qué migraciones adoptar (baseline) y aplicar. No muta la base."""
    migrations = discover_migrations(migrations_dir)
    applied = _load_applied(conn)
    _verify_applied(migrations, applied)

    if applied:
        to_apply = [item for item in migrations if item.version not in applied]
        return MigrationPlan(to_adopt=[], to_apply=to_apply)

    baseline = migrations[0]

    # Si hay metadata completa, la categoría "complete" no llega aquí. Sin
    # registro y sin objetos del baseline: fresh (aplicar todo). Parcial: error.
    state = _classify_against_baseline(conn, baseline)
    if state == "partial":
        raise MigrationError(
            "Schema PostgreSQL parcial/incompleto sin registro de migraciones; "
            "no se aplica el baseline automáticamente. Reparar o re-bootstrapear."
        )
    if state == "complete":
        logger.info(
            "Baseline %s detectado completo; se registra sin ejecutar (auto-baseline).",
            baseline.name,
        )
        return MigrationPlan(to_adopt=[baseline], to_apply=migrations[1:])

    return MigrationPlan(to_adopt=[], to_apply=list(migrations))


def _acquire_lock(conn, *, timeout: float | None = None) -> None:
    """Adquiere el advisory lock de sesión con reintentos acotados.

    Reintenta ``pg_try_advisory_lock`` cada ``_LOCK_POLL_INTERVAL`` hasta
    ``timeout`` (o ``_LOCK_WAIT_TIMEOUT`` si es ``None``). Mantiene la MISMA clave
    del bootstrap y no abre ventana de concurrencia: cada intento es atómico.
    """
    wait_limit = _LOCK_WAIT_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + wait_limit
    while True:
        acquired = conn.execute("SELECT pg_try_advisory_lock(%s)", (_PG_SCHEMA_LOCK,)).fetchone()
        conn.commit()
        if acquired and acquired[0]:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeout(
                "No se pudo adquirir el advisory lock de esquema tras "
                f"{wait_limit:.0f}s; otro runner o el bootstrap lo está reteniendo."
            )
        time.sleep(_LOCK_POLL_INTERVAL)


def _release_lock(conn) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_PG_SCHEMA_LOCK,))
        conn.commit()
    except Exception:  # pragma: no cover - cierre/abort ya libera la sesión
        try:
            conn.rollback()
        except Exception:
            pass


def _record(conn, migration: MigrationFile) -> None:
    try:
        conn.execute(
            f"INSERT INTO {METADATA_TABLE} (version, name, checksum) VALUES (%s, %s, %s)",
            (migration.version, migration.name, migration.checksum),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _execute(conn, migration: MigrationFile) -> None:
    """Aplica UNA migración y la registra en la misma transacción.

    Las sentencias BEGIN/COMMIT/ROLLBACK del archivo se descartan para no
    romper la atomicidad (el archivo 001 las incluye). Un error hace rollback de
    la transacción y NO registra la versión.
    """
    statements = split_sql_statements(migration.path.read_text(encoding="utf-8"))
    try:
        for statement in statements:
            stripped = statement.strip()
            if not stripped or stripped.upper() in _TRANSACTION_COMMANDS:
                continue
            conn.execute(stripped)
        conn.execute(
            f"INSERT INTO {METADATA_TABLE} (version, name, checksum) VALUES (%s, %s, %s)",
            (migration.version, migration.name, migration.checksum),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply_pending_migrations(
    conn,
    *,
    migrations_dir: Path | str = MIGRATIONS_PG_DIR,
    dry_run: bool = False,
    lock_timeout: float | None = None,
) -> MigrationResult:
    """Aplica las migraciones pendientes bajo advisory lock.

    La espera del advisory lock está acotada por ``lock_timeout`` (por defecto
    ``_LOCK_WAIT_TIMEOUT``): si otro proceso lo retiene más tiempo, se eleva
    ``MigrationLockTimeout`` en lugar de esperar indefinidamente.

    Con ``dry_run=True`` no escribe nada (ni siquiera la tabla de metadata): solo
    calcula el plan y lo reporta en ``pending``.
    """
    try:
        _acquire_lock(conn, timeout=lock_timeout)
        if not dry_run:
            _ensure_metadata_table(conn)

        plan = plan_migrations(conn, migrations_dir)

        if dry_run:
            return MigrationResult(
                dry_run=True,
                adopted=[],
                applied=[],
                pending=[*plan.to_adopt, *plan.to_apply],
                current_version=_current_version(conn),
            )

        for migration in plan.to_adopt:
            logger.info("Registrando baseline %s sin ejecutar (auto-baseline).", migration.name)
            _record(conn, migration)

        for migration in plan.to_apply:
            logger.info("Aplicando migración %s...", migration.name)
            _execute(conn, migration)
            logger.info("Migración %s aplicada.", migration.name)

        return MigrationResult(
            dry_run=False,
            adopted=list(plan.to_adopt),
            applied=list(plan.to_apply),
            pending=[],
            current_version=_current_version(conn),
        )
    finally:
        _release_lock(conn)


def run_migrations(
    conninfo: str,
    *,
    migrations_dir: Path | str = MIGRATIONS_PG_DIR,
    dry_run: bool = False,
    lock_timeout: float | None = None,
) -> MigrationResult:
    """Abre una conexión PostgreSQL dedicada y aplica las migraciones pendientes.

    ``lock_timeout`` acota la espera del advisory lock compartido (por defecto
    ``_LOCK_WAIT_TIMEOUT``) y se propaga a :func:`apply_pending_migrations`.
    """
    normalized = normalize_database_url(conninfo)
    if not normalized:
        raise MigrationError("conninfo PostgreSQL vacío")
    with psycopg.connect(normalized) as conn:
        return apply_pending_migrations(
            conn, migrations_dir=migrations_dir, dry_run=dry_run, lock_timeout=lock_timeout
        )
