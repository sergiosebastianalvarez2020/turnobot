# Línea base de auditoría — Migrador incremental PostgreSQL (`database/pg_migrator.py`)

## Encabezado

| Campo | Valor |
| --- | --- |
| Fecha de la auditoría | 2026-10-09, 21:37–21:45 (America/Buenos_Aires, UTC-3) |
| Fecha de guardado como línea base | 2026-10-09, 22:00 (America/Buenos_Aires, UTC-3) |
| Estado | **AUDITORÍA COMPLETADA — CORRECCIONES NO APLICADAS** |
| Alcance | Revisión de **solo lectura** del migrador incremental PostgreSQL y su CLI |
| Documento | `docs/PG_MIGRATOR_AUDIT_BASELINE.md` (nuevo; no existía previamente) |

### Aviso sobre P2-01 y aprobación para producción

La validación integral de `tests/` correspondiente a **P2-01 tiene su propia ejecución pendiente** y no fue ejecutada ni interferida por esta auditoría. Este documento **no constituye** una aprobación del migrador para producción: es una línea base de deuda técnica y hallazgos, para retomarse **después** de cerrar P2-01.

**Ningún hallazgo se marca como resuelto.** No se aplicó ninguna corrección y no debe interpretarse ninguna sección como un cambio de código.

### Restricciones respetadas durante la auditoría

- Solo lectura: no se modificó, creó ni eliminó ningún archivo de código, test, configuración o documentación.
- No se ejecutó pytest ni ninguna otra suite.
- No se ejecutaron comandos de migración, inicialización de bases ni SQL.
- No se inició ni detuvo Docker, procesos ni servicios.
- No se hizo commit ni push.
- No se interrumpió la validación de P2-01 en curso.

### Convención de clasificación

- **Confirmado** — la conclusión se sostiene directamente sobre el código inspeccionado.
- **Probable** — hay evidencia de código suficiente, pero el comportamiento final depende de semántica de psycopg3/PostgreSQL que no puede resolverse por lectura estática.
- **Pendiente de validación dinámica** — requiere ejecución contra PostgreSQL real para afirmarse o descartarse.

---

## 1. Archivos revisados e inexistentes

**Revisados**

| Archivo | Rol |
| --- | --- |
| `database/pg_migrator.py` | Objeto de la auditoría: runner versionado de esquema PostgreSQL |
| `scripts/migrate_pg.py` | CLI del runner |
| `database/database.py` | Bootstrap PostgreSQL (`_init_postgresql`), constante de lock compartida, runner SQLite |
| `database/pg_pool.py` | `normalize_database_url`, `sanitize_database_url`, `split_sql_statements`, `escape_literal_percent`, adaptador y proxies |
| `migrations_pg/001_initial_schema.sql` | Baseline del esquema PostgreSQL |
| `tests/test_pg_migrator.py` | 20 tests unitarios del runner (conexión simulada) |
| `tests_pg/test_pg_migrator_live.py` | 12 tests live del runner contra PostgreSQL real |
| `tests_pg/_helpers.py` | Helpers del harness PostgreSQL |
| `tests_pg/conftest.py` | Fixtures de la suite PostgreSQL |
| `tests/conftest.py` | Fixtures de la suite general (incluye el `skip` autouse) |
| `tests_pg/test_migrator.py` | Tests del ETL SQLite a PostgreSQL |
| `.github/workflows/tests.yml` | CI |
| `DEPLOYMENT.md` | Guía operativa, sección de migraciones |
| `README.md` | Documentación general, sección de migraciones de esquema |
| `docs/TEST_SUITE_INVENTORY.md` | Inventario canónico de tests |
| `pyproject.toml` | Configuración de pytest, ruff y mypy |
| `app.py`, `database/seed_auth.py`, `scripts/send_reminders.py`, `scripts/backup_postgresql.py` | Contexto de arranque y scripts operativos con los que se contrastó la CLI |

**Archivos inexistentes**

- Todos los archivos de la lista solicitada existen. En particular, `tests_pg/test_pg_migrator_live.py` existe con ese nombre exacto y con ese contenido.
- No existe ninguna variante con otro nombre (`tests_pg/test_migrator_live.py`, etc.).

**Contenido de `migrations_pg/`:** un único archivo, `001_initial_schema.sql`.

---

## 2. Resumen de lo ya implementado

### 2.1 Versionado

- **Identificación:** nombre de archivo `NNN_nombre.sql`. Regex `_MIGRATION_FILENAME_RE = ^(\d{3,})_([A-Za-z0-9_]+)\.sql$`; la versión es el `int` del prefijo numérico.
- **Orden:** `discovered.sort(key=lambda item: item.version)` → orden **numérico**, no lexicográfico.
- **Baseline:** `BASELINE_VERSION = 1`. Se exige que las versiones descubiertas sean exactamente `range(1, n+1)`: contiguas, sin huecos y sin duplicados. Cualquier desviación produce `MigrationError` **antes de tocar la base de datos**.
- **Checksum:** SHA-256 del contenido obtenido con `path.read_text(encoding="utf-8")`. Al leerse en modo texto con traducción universal de saltos de línea, CRLF y LF producen el mismo hash: no hay deriva de checksum por sistema operativo.
- **Tabla de control:** `schema_migrations(version BIGINT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`.

### 2.2 Integridad (alcance real de lo que se valida)

`_verify_applied` valida el registro aplicado contra los archivos en disco:

1. **Contigüidad del registro** desde `BASELINE_VERSION`; un registro no contiguo eleva `MigrationError`.
2. **Versiones desconocidas por el código** (registro más nuevo que el código, es decir downgrade) elevan `MigrationError` con el mensaje "La base tiene migraciones que el código no conoce".
3. **Checksum modificado** de una migración ya aplicada eleva `MigrationError` con el detalle de versión y nombre.

Cubre también los casos de "migración eliminada" mientras la eliminación deje el registro no contiguo o introduzca una versión desconocida. Ver §3 (D12) para las formas que **no** cubre.

### 2.3 Reconocimiento del esquema existente (auto-baseline)

Sin registro de metadata, `_classify_against_baseline` lee `001_initial_schema.sql`, extrae tablas e índices con las regex `_CREATE_TABLE_RE` / `_CREATE_INDEX_RE` (la **misma** detección que usa `database._init_postgresql`, duplicada en dos módulos) y los compara contra `information_schema.tables` (schema `public`, `BASE TABLE`) y `pg_indexes`:

| Clasificación | Condición | Acción |
| --- | --- | --- |
| `fresh` | No hay ningún objeto del baseline presente | Aplicar todo, empezando por 001 |
| `partial` | Algunos objetos presentes y faltan otros | `MigrationError`; no se aplica el baseline automáticamente |
| `complete` | Todos los objetos esperados presentes | **Adoptar la versión 1 sin ejecutarla** (`_record`) y continuar con `migrations[1:]` |

Limitación confirmada: si existe registro de metadata, **no** se vuelve a clasificar; el plan es simplemente "aplicar las versiones no registradas".

### 2.4 Transacciones

`_execute` aplica **una migración = una transacción**: descarta las sentencias `BEGIN`/`COMMIT`/`ROLLBACK` propias del archivo (para no romper la atomicidad; `001_initial_schema.sql` incluye `BEGIN;` y `COMMIT;`), ejecuta todas las sentencias restantes y luego el `INSERT` en `schema_migrations`, con **un único `commit`**. Ante cualquier error hace `rollback` y **no** registra la versión. Las migraciones anteriores ya confirmadas no se ven afectadas.

### 2.5 Concurrencia

- `_acquire_lock` usa `pg_try_advisory_lock` de **sesión** con la **misma clave** que el bootstrap: `_PG_SCHEMA_LOCK = _PG_SCHEMA_BOOTSTRAP_LOCK`, importada desde `database.database` (`6076853768564197445`).
- Espera acotada: reintento cada `_LOCK_POLL_INTERVAL = 0.25 s` hasta `_LOCK_WAIT_TIMEOUT = 60 s` (o el `lock_timeout` recibido); al vencer eleva `MigrationLockTimeout` en lugar de quedar colgado.
- El lock se adquiere **antes** de crear la tabla de metadata y se libera en un bloque `finally`, de modo que cubre la operación completa.
- El plan se recalcula **después** de adquirir el lock, así que un segundo runner observa lo ya registrado por el primero (comportamiento verificado por el test live de concurrencia).

### 2.6 Dry run

Con `dry_run=True`: se adquiere el lock, **no** se crea `schema_migrations`, no se ejecuta DDL y no se inserta registro. Solo se ejecutan lecturas (`to_regclass`, `information_schema`, `pg_indexes`, `COALESCE(MAX(version))`) y el resultado se reporta en `pending`. Verificado por test unitario (ausencia de `CREATE TABLE ... schema_migrations` en las llamadas) y por test live (base sin tablas tras el dry-run).

### 2.7 CLI

`scripts/migrate_pg.py` expone `--url` (default `os.environ["DATABASE_URL"]`), `--dry-run` y `--migrations-dir` (default `MIGRATIONS_PG_DIR`). Valida el backend con `get_database_backend` → `resolve_database_backend`; si no es PostgreSQL devuelve exit 1. Imprime el destino enmascarado con `sanitize_database_url`. `MigrationError` y excepciones inesperadas devuelven exit 1 (las inesperadas con `logger.error`, sin traza cruda). La docstring declara exit codes 0 y 1. Brechas conocidas: D3, D7, D8 y D13.

### 2.8 Pruebas

- `tests/test_pg_migrator.py`: **20 tests** con `_FakeConnection` (no requieren PostgreSQL).
- `tests_pg/test_pg_migrator_live.py`: **12 tests** con PostgreSQL real, marcados `@pytest.mark.pg_live`.
- Conteo verificado función por función contra el código.

### 2.9 Documentación

`DEPLOYMENT.md` (secciones "Migraciones PostgreSQL" y "Restricción del parser SQL") y `README.md` (sección "Schema migrations (PostgreSQL)") describen correctamente: versionado, checksum, detección de huecos y de downgrade, rollback por migración, auto-baseline del bootstrap, lock compartido con el arranque y espera acotada de 60 s configurable. Las divergencias están en §6.
---

## 3. Hallazgos D1–D17

Cada hallazgo indica archivo y función relevantes, y su clasificación. Los defectos confirmados se agrupan por subsistema (D1–D3, D5–D13) con el único riesgo probable (D4) señalado explícitamente en su encabezado; los pendientes de validación dinámica se agrupan en §3.3. El índice completo está en §3.4.

### 3.1 Defectos confirmados y el riesgo probable D4

#### D1 — (medio) Archivos `.sql` fuera del patrón de nombre se ignoran en silencio

**Clasificación: confirmado.**
**Archivo / función:** `database/pg_migrator.py` → `discover_migrations`.

El bucle de descubrimiento hace `continue` cuando el nombre no matchea `_MIGRATION_FILENAME_RE`, sin registrar advertencia ni elevar error:

```python
for path in directory.glob("*.sql"):
    match = _MIGRATION_FILENAME_RE.match(path.name)
    if match is None:
        continue
```

Consecuencia: una migración llamada `02_add_x.sql`, `2_add_x.sql`, `add_col.sql` o `001-extra.sql` **no se descubre**. El runner reporta "Sin migraciones pendientes" con exit code 0 y el DDL **nunca se aplica**. No hay ningún camino que avise al operador.

Esto contradice la propia docstring del módulo, que afirma: "Un hueco o **un nombre inválido** produce `MigrationError` antes de tocar la base de datos". Solo es cierto para nombres que sí matchean pero generan hueco o duplicado; para nombres fuera del patrón el comportamiento es el opuesto: silencio total.

Test que **afirma** el comportamiento silencioso: `tests/test_pg_migrator.py::test_discover_migrations_ignores_non_matching_files`.

#### D2 — (medio) El auto-baseline solo adopta la versión 1 y no detecta migraciones posteriores ya aplicadas

**Clasificación: confirmado.**
**Archivo / función:** `database/pg_migrator.py` → `plan_migrations` y `_classify_against_baseline`.

`_classify_against_baseline` examina **únicamente** los objetos declarados por `migrations[0]` (el baseline). Cuando clasifica `complete`, el plan es:

```python
return MigrationPlan(to_adopt=[baseline], to_apply=migrations[1:])
```

Escenario de fallo: se pierde o se borra `schema_migrations` en una base que ya tenía 001+002+003 aplicadas. Todos los objetos del baseline están presentes, la clasificación es `complete`, se adopta v1 y `to_apply = migrations[1:]` **reejecuta 002..N sobre objetos que ya existen**.

El fallo es *seguro* en el sentido de que no hay corrupción silenciosa: las migraciones son DDL no idempotente (`CREATE TABLE`), el error se produce y `_execute` hace `rollback` de esa migración, sin registrarla. Pero:

- el runner queda **incapaz de reconstruir la metadata** desde el estado real del esquema;
- no existe ninguna vía (`--adopt-baseline`, re-sembrado manual, `insert` guiado) para reparar la situación;
- el operador recibe un error de DDL ("relation already exists") en lugar de un diagnóstico explícito.

#### D3 — (medio, seguridad) `sanitize_database_url` no enmascara DSN en formato clave/valor

**Clasificación: confirmado.**
**Archivos / funciones:** `database/pg_pool.py` → `sanitize_database_url`; `scripts/migrate_pg.py` → `main`.

La función de enmascarado es un único `re.sub` sobre la forma URL:

```python
return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", url)
```

Solo cubre `esquema://usuario:contraseña@host`. La CLI acepta `--url` descrito en su propia docstring como "URL/DSN PostgreSQL" e imprime:

```python
print(f"Destino: {target}")
```

Un DSN en formato clave/valor (por ejemplo `host=db user=u password=secreto dbname=x`) se imprime **en claro en stdout**. Además, en la rama de excepción inesperada la CLI escribe el mensaje de psycopg:

```python
except Exception as error:
    logger.error("Fallo aplicando migraciones: %s", error)
```

El mensaje de `psycopg` puede arrastrar la cadena de conexión y `sanitize_database_url` no se aplica a esa rama.

Observación de contexto: `scripts/backup_postgresql.py` sí tiene el cuidado correcto para su caso, construye el entorno libpq con `PGPASSWORD` y ofrece `sanitize_pg_env` para enmascararlo. El runner no.

#### D4 — (medio) **RIESGO PROBABLE, NO CONFIRMADO**: `%` literal en una migración; la ruta del runner no aplica la duplicación que usa el resto del proyecto

**Clasificación: probable (requiere validación dinámica para confirmarse o descartarse).**
**Archivos / funciones:** `database/pg_migrator.py` → `_execute`; `database/database.py` → `_init_postgresql`; `database/pg_pool.py` → `escape_literal_percent`, `adapt_query_for_postgres`.

`_execute` ejecuta cada sentencia sobre la **conexión psycopg cruda**:

```python
conn.execute(stripped)
```

No pasa por `database.pg_pool.escape_literal_percent`, que es la pieza que el proyecto usa para duplicar `%` a `%%` antes de que psycopg3 parsee la sentencia. En cambio el bootstrap (`_init_postgresql` → `PgConnectionProxy.execute` → `adapt_query_for_postgres`) y todas las queries de la aplicación **sí** aplican esa duplicación.

Es decir, la ruta del runner y la ruta del bootstrap tratan `%` de forma **distinta**, y como mucho una de las dos puede ser correcta:

- Si psycopg3 interpreta placeholders aun con `params=None`, entonces un `%` literal en el DDL de una migración (por ejemplo dentro de un `COMMENT`, un `DEFAULT`, un `LIKE` o un índice parcial con patrón) hará fallar la migración con `ProgrammingError`.
- Si psycopg3 **no** los interpreta, entonces el adaptador estaría enviando `%%` literal y corrompiendo texto con `%` en el bootstrap.

Ninguna de las dos ramas tiene cobertura: `001_initial_schema.sql` no contiene ningún `%`, y tampoco las migraciones de los tests.

#### D5 — (bajo) `_release_lock` puede dejar el advisory lock de sesión retenido

**Clasificación: confirmado.**
**Archivo / función:** `database/pg_migrator.py` → `_release_lock`.

```python
def _release_lock(conn) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_PG_SCHEMA_LOCK,))
        conn.commit()
    except Exception:  # pragma: no cover - cierre/abort ya libera la sesión
        try:
            conn.rollback()
        except Exception:
            pass
```

Si el `pg_advisory_unlock` no llega a ejecutarse, el lock de **sesión** sigue tomado hasta que la conexión se cierre. En el flujo de `run_migrations` la conexión es dedicada y muere inmediatamente después, así que el impacto real es nulo.

El riesgo aparece con `apply_pending_migrations(conn)`, que es API pública del módulo: si se la invoca con una conexión de pool (un `PgConnectionProxy` obtenido de `pg_pool`), el lock **sobrevive al `putconn`** y todos los runners posteriores fallarían con `MigrationLockTimeout` a los 60 s hasta que esa conexión se recicle o se cierre.

No hay test que ejercite un fallo con la conexión en estado abortado en el momento del unlock. El comentario `# pragma: no cover` en la rama de excepción reconoce implícitamente que esa ruta no está cubierta.

#### D6 — (bajo) Una ejecución que falla la validación igual escribe en la base

**Clasificación: confirmado.**
**Archivo / función:** `database/pg_migrator.py` → `apply_pending_migrations`, `_ensure_metadata_table`.

El orden real de operaciones es:

```python
_acquire_lock(conn, timeout=lock_timeout)
if not dry_run:
    _ensure_metadata_table(conn)     # CREATE TABLE IF NOT EXISTS + COMMIT
plan = plan_migrations(conn, migrations_dir)   # puede elevar MigrationError
```

`_ensure_metadata_table` ejecuta el DDL y hace `commit()` **antes** de que `plan_migrations` valide. Si la validación luego falla (por ejemplo `MigrationError` por "esquema parcial/incompleto sin registro"), la base ya quedó mutada por una corrida que reportó error.

La detección no se rompe (una tabla `schema_migrations` vacía no altera la clasificación `fresh`/`partial`/`complete`, porque no pertenece a los objetos esperados del baseline), pero es una mutación observable en un camino documentado como libre de efectos.

Además contradice literalmente `DEPLOYMENT.md`, que afirma: "Un esquema parcial produce un error explícito **sin tocar DDL**".

#### D7 — (bajo) La CLI no expone `lock_timeout`

**Clasificación: confirmado.**
**Archivo / función:** `scripts/migrate_pg.py` → `build_parser`.

`run_migrations` y `apply_pending_migrations` aceptan `lock_timeout`, y `DEPLOYMENT.md` lo documenta como "configurable con `lock_timeout` en `run_migrations`/`apply_pending_migrations`". Pero el parser solo define `--url`, `--dry-run` y `--migrations-dir`. El único camino operativo documentado queda fijado en `_LOCK_WAIT_TIMEOUT = 60 s`, sin forma de ajustarlo sin escribir código Python.

Relacionado: no existe variable de entorno ni entrada de configuración para ese timeout; `_LOCK_WAIT_TIMEOUT` es una constante de módulo modificable solo por monkeypatch en tests.

#### D8 — (bajo) `scripts/migrate_pg.py` no carga `.env`

**Clasificación: confirmado.**
**Archivo / función:** `scripts/migrate_pg.py` (módulo completo).

El script no importa ni invoca `load_dotenv()`. En cambio:

- `app.py` sí lo hace (`from dotenv import load_dotenv`; `load_dotenv()` antes de crear la aplicación);
- `scripts/send_reminders.py` sí lo hace;
- `scripts/backup_postgresql.py` **no** lo hace (usa `os.environ.get("DATABASE_URL")`), así que el repositorio es inconsistente entre scripts operativos.

Impacto concreto: `README.md` documenta

```bash
python scripts/migrate_pg.py --dry-run
python scripts/migrate_pg.py
```

sin `--url`. En un host donde `DATABASE_URL` vive solo en `.env`, esos comandos abortan con `ERROR: DATABASE_URL no definida; use --url.` y exit 1. `DEPLOYMENT.md` sí pasa `--url "$env:DATABASE_URL"`, así que las dos documentaciones no describen el mismo procedimiento.

#### D9 — (bajo) Los tests unitarios del runner se saltan cuando no hay PostgreSQL

**Clasificación: confirmado.**
**Archivos:** `tests/test_pg_migrator.py`, `tests/conftest.py`.

`tests/test_pg_migrator.py` usa exclusivamente `_FakeConnection` y **no necesita un servidor**. Sin embargo vive en `tests/`, y `tests/conftest.py` define un fixture autouse que omite todo el módulo cuando falta la variable:

```python
@pytest.fixture(autouse=True)
def pg_test_env(monkeypatch):
    if not TURNOBOT_PG_URL:
        pytest.skip("TURNOBOT_PG_URL no está definida; suite funcional PostgreSQL omitida.")
```

Consecuencia: sin `TURNOBOT_PG_URL`, los 20 tests de lógica pura del runner (descubrimiento, plan fresh/complete/partial, checksum, huecos, registro no contiguo, dry-run, rollback, timeout del lock) **no se ejecutan**. La cobertura del núcleo de decisión del migrador queda condicionada a disponer de PostgreSQL, aunque el código bajo prueba no lo requiera.
#### D10 — (bajo) Política de nombres frágil por ancho de dígitos variable

**Clasificación: confirmado.**
**Archivo / función:** `database/pg_migrator.py` → `_MIGRATION_FILENAME_RE`, `discover_migrations`.

El prefijo se acepta con `\d{3,}` (tres o más dígitos), por lo que dos archivos distintos pueden resolver a la misma versión lógica: `0001_x.sql` y `001_x.sql` producen ambos versión `1`. En ese caso se detecta el duplicado y se eleva `MigrationError`, lo cual es el comportamiento correcto.

El problema es el otro extremo: un archivo realista como `2024_01_01_fix.sql` matchea el patrón (`2024` más `01_01_fix`), se interpreta como **versión 2024** y produce un error de "Huecos o versiones no contiguas" en lugar de ser ignorado o rechazado por nombre. Combinado con D1 (nombres fuera de patrón ignorados en silencio frente a nombres con prefijo numérico interpretados como versión), el comportamiento ante nombres irregulares no es predecible ni está documentado.

#### D11 — (bajo) La verificación de "esquema completo" es solo por nombre de tabla e índice

**Clasificación: confirmado.**
**Archivos / funciones:** `database/pg_migrator.py` → `_classify_against_baseline`, `_expected_objects`; `database/database.py` → `_init_postgresql`.

Tanto la clasificación del baseline como el bootstrap comparan **conjuntos de nombres**: `_expected_objects` extrae con regex los nombres tras `CREATE TABLE` y `CREATE (UNIQUE) INDEX` del archivo SQL, y se contrastan contra `information_schema.tables` (nombre de tabla) y `pg_indexes` (nombre de índice). No se verifica ninguna columna, `CHECK`, `NOT NULL`, clave foránea, `UNIQUE` ni la definición real del índice.

Consecuencia: una base a la que le falta una columna, un `CHECK`, una clave foránea o la definición parcial de un índice, pero que conserva **todos los nombres** esperados, se clasifica `complete`. En esa situación:

- `_init_postgresql` registra "Schema PostgreSQL completo; se omite bootstrap." y hace `rollback` sin aplicar nada;
- `pg_migrator` adopta la versión 1 como baseline y avanza sobre un esquema que solo está completo en apariencia.

Es un falso positivo de integridad: el sistema declara el esquema sano sin haberlo verificado más allá de los nombres.

#### D12 — (bajo) Brecha de auditoría: `_verify_applied` nunca compara `name`

**Clasificación: confirmado.**
**Archivo / función:** `database/pg_migrator.py` → `_verify_applied`, `_load_applied`.

La validación compara únicamente `version` (contigüidad y existencia) y `checksum`. La columna `name`, que se persiste justamente para auditoría, se carga en `_load_applied` pero **no se usa en ninguna comprobación**:

```python
rows = conn.execute(
    f"SELECT version, name, checksum FROM {METADATA_TABLE} ORDER BY version"
).fetchall()
return {int(row[0]): {"name": row[1], "checksum": row[2]} for row in rows}
```

Renombrar un archivo sin cambiar su contenido (`002_widgets.sql` a `002_otro_nombre.sql`) pasa la validación: el checksum coincide y la versión coincide. La tabla de control sigue afirmando el nombre antiguo, de modo que la traza de auditoría puede mentir sobre qué archivo se aplicó realmente.

#### D13 — (bajo, seguridad/operación) `--migrations-dir` permite aplicar DDL arbitrario contra producción

**Clasificación: confirmado.**
**Archivo / función:** `scripts/migrate_pg.py` → `build_parser`, `main`.

El flag acepta cualquier ruta:

```python
parser.add_argument(
    "--migrations-dir",
    default=str(MIGRATIONS_PG_DIR),
    help="Directorio de migraciones .sql (por defecto migrations_pg/)",
)
```

No hay restricción de que el directorio esté bajo `BASE_DIR`, ni un flag explícito de habilitación, ni confirmación. La combinación `--url` apuntando a producción con `--migrations-dir <ruta arbitraria>` permite aplicar SQL de un directorio ajeno contra la base productiva. La capacidad es útil en desarrollo y en tests (`tests_pg/test_pg_migrator_live.py` la usa con `tmp_path`), por eso el hallazgo es de operación y seguridad y no un bug funcional: lo que falta es separar la capacidad de desarrollo del camino documentado hacia producción.

### 3.2 Riesgos probables (no confirmados)

Un único hallazgo queda en esta categoría:

- **D4** — `%` literal en una migración. Está documentado arriba, en §3.1, agrupado por subsistema y con su encabezado marcado explícitamente como **RIESGO PROBABLE, NO CONFIRMADO**. La razón de la clasificación: la afirmación "la migración falla con `ProgrammingError`" y su contraria ("el adaptador corrompe `%` literales") son mutuamente excluyentes y no pueden dirimirse leyendo el código; requieren ejecutar una sentencia con `%` contra PostgreSQL real, tanto por la ruta del runner (`conn.execute` con `params=None`) como por la ruta del adaptador.

### 3.3 Pendientes de validación dinámica

Los siguientes puntos están confirmados **como código**, pero su efecto operativo no puede afirmarse sin ejecución contra PostgreSQL real.

#### D14 — Exclusión runner contra bootstrap: no está probada la intersección sesión/transacción

**Clasificación: pendiente de validación dinámica.**
**Archivos / funciones:** `database/pg_migrator.py` → `_acquire_lock`; `database/database.py` → `_init_postgresql`.

El runner toma el lock con `pg_try_advisory_lock` (**sesión**); `database._init_postgresql` toma el mismo `bigint` con `pg_advisory_xact_lock` (**transacción**). Toda la promesa de exclusión runner contra bootstrap depende de que ambas familias de un argumento compartan espacio de nombres. No puede probarse por lectura estática, y la cobertura existente **no** ejercita ese cruce: `tests_pg/test_pg_migrator_live.py::test_lock_wait_times_out_when_lock_is_held_then_recovers` bloquea con `pg_advisory_lock` (sesión contra sesión), nunca con `pg_advisory_xact_lock`.

#### D15 — La conexión del runner no hereda configuración de sesión ni tiene `statement_timeout`

**Clasificación: pendiente de validación dinámica.**
**Archivos / funciones:** `database/pg_migrator.py` → `run_migrations`; `database/pg_pool.py` → `configure_pg_session`, `PG_LOCK_TIMEOUT`.

`run_migrations` abre la conexión con `psycopg.connect(normalized)` y **no** aplica `pg_pool.configure_pg_session`, que es donde el pool fija `TimeZone` y `lock_timeout = 5s`. No hay `statement_timeout` ni ningún otro límite: la **espera del advisory lock** está acotada (60 s o `lock_timeout`), pero el **DDL en sí no lo está**. Un `ALTER TABLE` largo o bloqueado por otra transacción puede colgar el runner indefinidamente, y no hay timeout de aplicación que lo corte.

#### D16 — `CREATE INDEX CONCURRENTLY` es inejecutable por diseño (restricción no documentada)

**Clasificación: pendiente de validación dinámica.**
**Archivo / función:** `database/pg_migrator.py` → `_execute`.

Cada migración se aplica en una única transacción. `CREATE INDEX CONCURRENTLY` (y `DROP INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`) no pueden ejecutarse dentro de un bloque transaccional, así que una migración que los use fallará siempre. `DEPLOYMENT.md` documenta otras restricciones del parser (`$...$`, `SET TRANSACTION`, `SAVEPOINT`) pero **no** menciona ésta.

#### D17 — La clave del advisory lock es global del cluster, no por base de datos

**Clasificación: pendiente de validación dinámica.**
**Archivos / funciones:** `database/pg_migrator.py` → `_PG_SCHEMA_LOCK`; `database/database.py` → `_PG_SCHEMA_BOOTSTRAP_LOCK`.

`_PG_SCHEMA_LOCK` es una única constante (el mismo `bigint` que el bootstrap). El lock es de sesión y de alcance por servidor: dos runners ejecutándose contra **bases distintas** del mismo servidor PostgreSQL se serializan entre sí, y un bootstrap lento en cualquier base del cluster bloquea a todos los runners del cluster hasta 60 s. Confirmado en código; el impacto real depende de la topología de despliegue y no puede medirse sin ejecución.

### 3.4 Índice de clasificación de los hallazgos

| ID | Título corto | Clasificación |
| --- | --- | --- |
| D1 | `.sql` fuera de patrón ignorado en silencio | Confirmado |
| D2 | Auto-baseline solo adopta v1; metadata irrecuperable con N mayor que 1 | Confirmado |
| D3 | Fuga de credenciales por DSN clave/valor | Confirmado |
| D4 | `%` literal divergente entre runner y bootstrap | **Probable** |
| D5 | `_release_lock` puede dejar el lock de sesión retenido | Confirmado |
| D6 | Un run fallido igual crea `schema_migrations` | Confirmado |
| D7 | La CLI no expone `lock_timeout` | Confirmado |
| D8 | La CLI no carga `.env` | Confirmado |
| D9 | Los tests unitarios del runner se saltan sin PostgreSQL | Confirmado |
| D10 | Ancho de dígitos variable en el nombre | Confirmado |
| D11 | "Esquema completo" verificado solo por nombre | Confirmado |
| D12 | `_verify_applied` no compara `name` | Confirmado |
| D13 | `--migrations-dir` permite DDL arbitrario contra producción | Confirmado |
| D14 | Exclusión runner contra bootstrap no probada | Pendiente de validación dinámica |
| D15 | Sin `statement_timeout` en la conexión del runner | Pendiente de validación dinámica |
| D16 | `CREATE INDEX CONCURRENTLY` inejecutable y no documentado | Pendiente de validación dinámica |
| D17 | Clave de lock global del cluster | Pendiente de validación dinámica |

Resumen: **12 confirmados** (D1, D2, D3, D5, D6, D7, D8, D9, D10, D11, D12, D13), **1 probable** (D4) y **4 pendientes de validación dinámica** (D14, D15, D16, D17).
---

## 4. Pruebas: qué existe y qué falta

### 4.1 Cobertura sin PostgreSQL — `tests/test_pg_migrator.py` (20 tests)

Todos usan `_FakeConnection`, una conexión DB-API simulada. No requieren servidor.

| Test | Qué verifica |
| --- | --- |
| `test_discover_migrations_orders_by_version_and_hashes` | Orden numérico por versión y checksum SHA-256 |
| `test_discover_migrations_rejects_gap` | Hueco de versión eleva `MigrationError` |
| `test_discover_migrations_rejects_empty_directory` | Directorio sin migraciones eleva `MigrationError` |
| `test_discover_migrations_ignores_non_matching_files` | Archivos fuera de patrón se **ignoran** (afirma D1) |
| `test_plan_fresh_database_applies_baseline` | Base vacía: se aplica el baseline |
| `test_plan_complete_baseline_is_adopted` | Baseline completo sin registro: se adopta |
| `test_plan_partial_baseline_raises` | Esquema parcial (solo tabla) eleva `MigrationError` |
| `test_plan_partial_when_only_index_present_raises` | Esquema parcial (solo índice) eleva `MigrationError` |
| `test_plan_pending_only_new_versions` | Solo se planifican las versiones no registradas |
| `test_plan_detects_checksum_change` | Checksum distinto eleva `MigrationError` |
| `test_plan_detects_unknown_applied_version` | Versión registrada que el código no conoce eleva error |
| `test_plan_detects_non_contiguous_registry` | Registro no contiguo eleva error |
| `test_dry_run_writes_nothing` | Dry-run no crea la tabla de metadata |
| `test_apply_executes_and_records` | Aplica la migración y la registra |
| `test_apply_adopts_complete_baseline_then_applies_next` | Adopta v1 y aplica v2 |
| `test_apply_rolls_back_and_does_not_record_on_failure` | Fallo hace rollback y no registra |
| `test_migration_lock_timeout_is_migration_error` | `MigrationLockTimeout` es subclase de `MigrationError` |
| `test_acquire_lock_timeout_raises_without_touching_schema` | Timeout del lock sin tocar el esquema |
| `test_acquire_lock_uses_default_timeout_when_not_specified` | Timeout por defecto |
| `test_acquire_lock_success_uses_shared_session_key` | Se usa la clave de lock compartida |

### 4.2 Cobertura con PostgreSQL real — `tests_pg/test_pg_migrator_live.py` (12 tests)

Marcados `@pytest.mark.pg_live`; usan bases desechables `turnobot_test_<hex>`.

| Test | Qué verifica |
| --- | --- |
| `test_applies_baseline_on_empty_database_and_is_idempotent` | Baseline en base vacía e idempotencia |
| `test_adopts_existing_baseline_without_reexecution` | Auto-baseline sobre esquema bootstrapeado |
| `test_applies_incremental_migrations_in_order` | Orden incremental 001+002 en una corrida |
| `test_incremental_after_existing_baseline` | Incremental tras adopción del baseline |
| `test_checksum_mismatch_fails_before_applying` | Checksum distinto falla y no aplica |
| `test_version_gap_fails_without_schema_changes` | Hueco falla sin modificar el esquema |
| `test_partial_schema_without_metadata_fails` | Esquema parcial sin metadata falla |
| `test_failed_migration_rolls_back_and_is_not_recorded` | Rollback de la migración fallida |
| `test_dry_run_writes_nothing` | Dry-run no deja tablas |
| `test_concurrent_runners_serialize` | Dos runners en paralelo se serializan |
| `test_lock_wait_times_out_when_lock_is_held_then_recovers` | Timeout del lock y recuperación |
| `test_cli_dry_run_and_invalid_url` | CLI en dry-run y URL no PostgreSQL |

### 4.3 Qué necesita PostgreSQL real

- **Obligatorio PostgreSQL:** adopción de baseline sobre un esquema real, ejecución y rollback de DDL, conflicto efectivo de advisory locks, dry-run contra catálogos reales, CLI completo.
- **No requiere PostgreSQL:** descubrimiento de archivos, validación de checksum, huecos, registro no contiguo, cálculo del plan y el timeout del lock con conexión simulada.

### 4.4 Gaps de cobertura, mapeados a cada defecto

| Gap | Defecto asociado |
| --- | --- |
| Ningún test con `%` literal en el SQL de una migración | D4 |
| Ningún test de `_release_lock` con transacción abortada | D5 |
| Ningún test de auto-baseline con más de una migración ya aplicada y metadata perdida | D2 |
| Ningún test que exija aviso o error para un `.sql` fuera de patrón | D1 |
| Ningún test de ancho de prefijo (`0001_` frente a `001_`, `2024_01_01_`) | D10 |
| Ningún test de `run_migrations("")` elevando `MigrationError` | — |
| Ningún test de que un run fallido crea `schema_migrations` | D6 |
| Ningún test del CLI con DSN clave/valor ni del exit code ante `MigrationError` | D3 |
| El test de lock bloquea con `pg_advisory_lock` de sesión, nunca con `pg_advisory_xact_lock` | D14 |
| Ningún test de `--migrations-dir` apuntando fuera del repositorio | D13 |
| Ningún test de `name` renombrado con checksum idéntico | D12 |
| Ningún test que verifique columnas o constraints (no solo nombres) al clasificar `complete` | D11 |
| Ningún test de `statement_timeout` en la conexión del runner | D15 |
| Ningún test de `CREATE INDEX CONCURRENTLY` | D16 |
| Ningún test de dos bases distintas del mismo servidor serializadas por la clave global | D17 |

---

## 5. Riesgos de integridad y seguridad

Ordenados por prioridad de tratamiento.

1. **D3 (seguridad)** — fuga de contraseña por DSN en formato clave/valor, tanto en `stdout` como en el log de error del CLI.
2. **D4 (integridad, probable)** — tratamiento divergente de `%` entre la ruta que aplica migraciones y la que hace el bootstrap; un literal `%` en DDL puede romper la migración, o el bootstrap puede corromperlo. Como mucho una de las dos rutas es correcta.
3. **D2 (integridad y recuperación)** — pérdida de `schema_migrations` en una base con más de una migración deja la metadata irrecuperable por el runner y provoca la re-ejecución de DDL ya aplicado. Falla de forma segura, pero sin vía de reparación.
4. **D5 (integridad)** — lock de sesión que puede sobrevivir al ciclo de vida de una conexión de pool, con bloqueo permanente de todos los runners posteriores.
5. **D6 (contrato)** — mutación de la base en una corrida que reporta fallo de validación, contradiciendo la documentación.
6. **D13 (operación)** — `--migrations-dir` permite aplicar SQL de un directorio arbitrario contra producción.
7. **D11 (falso positivo de integridad)** — "esquema completo" verificado solo por nombres, lo que permite adoptar el baseline u omitir el bootstrap sobre un esquema incompleto en columnas o constraints.
8. **D14 y D17 (semántica no probada)** — la exclusión runner contra bootstrap y el alcance global de la clave del lock no están cubiertas por ningún test.
9. **D15 (disponibilidad)** — el DDL del runner no está acotado por ningún timeout de aplicación.
10. **D16 (documentación y operación)** — `CREATE INDEX CONCURRENTLY` es inejecutable por diseño y esa restricción no está documentada.
11. **D1, D7, D8, D9, D10, D12 (bajo)** — silencio ante nombres inválidos, falta de flag de timeout, inconsistencia de `.env` entre scripts, cobertura condicionada a PostgreSQL y brechas de auditoría del registro.

### 5.1 Límites de esta revisión estática

Lo siguiente **no** pudo resolverse leyendo código y queda explícitamente como duda abierta:

- Semántica exacta del cruce `pg_try_advisory_lock` (sesión) con `pg_advisory_xact_lock` (transacción) sobre la misma clave `bigint` (D14).
- Interpretación de `%` por parte de psycopg3 cuando `params=None`, que decide si D4 es un defecto del runner o del adaptador.
- Efecto operativo real de la clave de lock global sobre un cluster con varias bases (D17).
- Comportamiento del lock cuando el proceso muere violentamente mientras lo retiene: la liberación depende de que PostgreSQL cierre la sesión, lo cual es esperable pero no verificable sin ejecución.
---

## 6. Discrepancias entre documentación e implementación

### 6.1 Afirmaciones documentales que coinciden con el código

Verificadas una por una contra `database/pg_migrator.py` y `scripts/migrate_pg.py`:

- Versionado por `migrations_pg/00N_nombre.sql` aplicado en orden determinista.
- Cada migración en su propia transacción, con rollback si falla.
- Nunca repite una migración ya aplicada.
- Falla ante checksum modificado, huecos de versión y registro más nuevo que el código.
- En una base vacía aplica `001_initial_schema.sql` y lo registra.
- Si la base ya fue bootstrapeada (001 completo, sin `schema_migrations`), registra el baseline sin re-ejecutarlo.
- Advisory lock compartido con el bootstrap, con espera acotada por defecto de 60 s configurable vía `lock_timeout`, y error `MigrationLockTimeout` en lugar de bloqueo indefinido.
- El registro vive en `schema_migrations(version, name, checksum, applied_at)`.
- Restricciones del parser SQL: sin cuerpos procedimentales `$...$`, sin `SET TRANSACTION` ni `SAVEPOINT`, y el runner descarta `BEGIN`/`COMMIT`/`ROLLBACK` del archivo.

### 6.2 Discrepancias confirmadas

1. **DEPLOYMENT.md: "Un esquema parcial produce un error explícito sin tocar DDL".** No coincide: `_ensure_metadata_table` crea y **commitea** `schema_migrations` antes de que `plan_migrations` detecte el esquema parcial (D6). La base sí queda tocada.
2. **DEPLOYMENT.md no menciona `CREATE INDEX CONCURRENTLY`.** La sección de restricciones del parser lista `$...$`, `SET TRANSACTION` y `SAVEPOINT`, pero omite que la transacción única por migración vuelve inejecutables `CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY` y `REINDEX CONCURRENTLY` (D16).
3. **DEPLOYMENT.md no menciona el alcance de la clave del lock.** Ofrece `lock_timeout` como configurable y describe la serialización, pero no advierte que la clave es global del cluster y no por base de datos, de modo que dos runners sobre bases distintas del mismo servidor se serializan entre sí (D17).
4. **DEPLOYMENT.md documenta `lock_timeout` como configurable, pero la CLI no lo expone.** El operador no tiene camino documentado para ajustarlo: `build_parser` no define el flag (D7). La constante `_LOCK_WAIT_TIMEOUT` tampoco tiene variable de entorno.
5. **README.md documenta `python scripts/migrate_pg.py --dry-run` sin `--url`,** mientras la CLI resuelve la URL exclusivamente de `os.environ["DATABASE_URL"]` y no carga `.env` (D8). DEPLOYMENT.md sí pasa `--url "$env:DATABASE_URL"`, así que las dos guías describen procedimientos distintos para el mismo comando.
6. **Cabecera de `migrations_pg/001_initial_schema.sql` desactualizada.** Afirma que el DDL equivale al estado final de "las 22 migraciones en `migrations/` (001..022)", pero el propio archivo incluye bloques comentados con etiquetas `-- 023:` y `-- 024:`, y `migrations/` contiene 23 archivos. `README.md` dice correctamente "(001–023)". La cabecera quedó atrás.
7. **Docstring de `database/pg_migrator.py`: "un nombre inválido produce `MigrationError`".** No es exacto: los nombres fuera del patrón se ignoran en silencio; solo los nombres que matchean y producen hueco o duplicado elevan error (D1).
8. **`README.md` declara `tests/` con "59 test files, 787 tests",** etiquetado como conteo histórico, mientras `docs/TEST_SUITE_INVENTORY.md` documenta 90 archivos y 1.284 items para el total. Es una inconsistencia de cifras ya reconocida en el propio README, pero conviene registrarla.

### 6.3 Observación de contexto (no es un defecto del migrador)

`database/seed_auth.py` y el bootstrap no sincronizan las secuencias IDENTITY de `roles` (el `INSERT INTO roles (id, name) VALUES (1, ...)` de `001_initial_schema.sql` no avanza la secuencia). El harness de tests lo corrige con `tests_pg/_helpers.py::sync_identity_sequences`, pero **esa sincronización vive solo en el harness de tests**, no en el camino de producción. Queda anotado como observación lateral; no se verificó su impacto en producción y no forma parte de los hallazgos D1–D17.

---

## 7. Propuestas de corrección priorizadas

**Ninguna corrección fue aplicada.** Se listan como propuesta, ordenadas por prioridad.

### P1 — Integridad y seguridad

1. **D3 — Enmascarar DSN en formato clave/valor.** Extender `sanitize_database_url` (`database/pg_pool.py`) para cubrir `password=...` y `pwd=...` en DSN clave/valor, y aplicarla también al mensaje de error del CLI (`scripts/migrate_pg.py`), no solo al `print` del destino.
2. **D2 — No adoptar el baseline a ciegas.** En `plan_migrations`, cuando el estado es `complete` y no hay metadata, verificar además que **no existan objetos declarados por `migrations[1:]`**; si existen, elevar un `MigrationError` explicativo ("esquema posterior detectado sin metadata; reconstruir `schema_migrations` manualmente"). Alternativa mínima: exigir un flag o parámetro explícito de adopción.
3. **D4 — Unificar la política de `%`.** Decidir el comportamiento y hacerlo consistente entre la ruta del runner y la del bootstrap, ya sea aplicando el mismo tratamiento que `escape_literal_percent` en `_execute`, o prohibiendo y documentando `%` literal. Requiere validación dinámica previa para saber cuál de las dos rutas es la correcta.

### P2 — Operación

4. **D6 — Mover `_ensure_metadata_table` después de `plan_migrations`,** o integrarla en la transacción que registra, de modo que una validación fallida no mute la base.
5. **D1 y D10 — Rechazar o advertir ante nombres irregulares.** Sustituir el `continue` silencioso por un WARNING más `MigrationError` cuando el archivo contenga DDL y no matchee el patrón; y fijar el ancho del prefijo a exactamente tres dígitos, o rechazar explícitamente los prefijos de otro ancho para que `2024_01_01_fix.sql` no se interprete como versión 2024.
6. **D7 — Agregar `--lock-timeout` a la CLI** y propagarlo a `run_migrations`.
7. **D8 — Agregar `load_dotenv()` a `migrate_pg.py`** y definir el patrón único para el resto de los scripts operativos, dado que `backup_postgresql.py` tampoco carga `.env`.
8. **D13 — Restringir `--migrations-dir`** a un subdirectorio de `BASE_DIR`, o exigir un flag explícito de habilitación para rutas externas.

### P3 — Auditoría, robustez y cobertura de tests

9. **D9 — Sacar `tests/test_pg_migrator.py` del `skip` autouse** (o moverlo a una suite no condicionada a `TURNOBOT_PG_URL`): su lógica no necesita servidor.
10. **D11 — Reforzar la detección de `complete`** con un puñado de columnas y constraints clave del baseline, o documentar explícitamente que la comprobación es solo por nombre.
11. **D12 — Comparar también `name` en `_verify_applied`** para detectar renombrados sin cambio de checksum.
12. **D15 — Fijar `statement_timeout` en la conexión del runner** y documentarlo.
13. **D16 — Documentar en `DEPLOYMENT.md`** que `CREATE INDEX CONCURRENTLY` no es aplicable con este runner.
14. **D14 — Agregar un test live que bloquee con `pg_advisory_xact_lock`** (la forma que usa `_init_postgresql`) y verifique que el runner falla con `MigrationLockTimeout`. El test actual cubre solo el cruce sesión contra sesión.
15. **Tests nuevos, uno por hallazgo del §4.4**, empezando por: `%` literal, `_release_lock` con transacción abortada, auto-baseline con más de una migración y metadata perdida, nombres fuera de patrón, DSN clave/valor en el CLI y exit code ante `MigrationError`.

---

## 8. Verificación final de este documento

| Comprobación | Resultado |
| --- | --- |
| Hallazgos D1–D17 presentes | Sí: D1–D13 en §3.1, D4 referenciado en §3.2, D14–D17 en §3.3 |
| Distinción confirmado / probable / pendiente | Sí: §3.2 (probable) y §3.3 (pendiente) separados, con índice por clasificación en §3.4 |
| Pruebas existentes | Sí: §4.1 (20 unitarias) y §4.2 (12 live), enumeradas por nombre |
| Pruebas faltantes | Sí: §4.4, cada gap mapeado a su defecto |
| Discrepancias documentales | Sí: §6.2, ocho discrepancias concretas |
| Propuestas priorizadas P1, P2 y P3 | Sí: §7, con prioridad y sin marcar nada como resuelto |
| Riesgos de integridad y seguridad | Sí: §5, once riesgos ordenados |
| Límites de la revisión estática | Sí: §5.1 |

### Nota sobre contenido agregado en este guardado

Todo el contenido de §1 a §7 proviene del informe de auditoría entregado; no se agregaron verificaciones nuevas ni resultados inventados. Las únicas adiciones de este guardado son estructurales y de clasificación:

- el encabezado con fecha, estado, alcance y el aviso sobre P2-01;
- la separación explícita en §3.2 (riesgos probables), §3.3 (pendientes de validación dinámica) y §3.4 (índice de clasificación);
- los rótulos **Clasificación:** en cada hallazgo;
- el §4.3 y el §4.4 (qué necesita PostgreSQL y mapa de gaps de cobertura);
- el §5 ordenado por prioridad, el §5.1 de límites, el §6.3 de observación de contexto y el §8 de verificación.

Ninguna corrección de código fue aplicada, no se modificó ningún archivo fuera de este documento, y no se ejecutó ninguna prueba, migración, SQL ni contenedor.
