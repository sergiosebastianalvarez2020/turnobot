# AGENTS.md — Project Guidelines

## Lint & Typecheck

- Python lint: `python -m ruff check .` (if ruff is installed)
- Typecheck: `python -m mypy app.py` (if mypy is installed)

## Test

- Run tests: `python -m pytest tests/ tests_pg/ -v --tb=short`
  (la suite `tests_pg/` y los tests funcionales PG se omiten si `TURNOBOT_PG_URL`
  no está definida)
- Coverage is enabled by default via pyproject.toml configuration

## Pre-existing Test Failures (Baseline)

The following tests fail on Windows due to platform limitations, unrelated to feature work:

1. `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json`
   - Failure cause: Windows does not support symlinks without elevated privileges
   - This test relies on symlink behavior for appointment rescheduling

2. `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted`
   - Failure cause: Windows symlink limitations (same as above)
   - This test validates backup symlink preservation during prune operations

**Expected baseline:** el run completo verificado más reciente (CI) es **1.284 passed / 0 failed / 0 skipped / 0 errores** sobre el commit `c3fee3a` (`feat: add versioned PostgreSQL migration runner`, workflow `tests.yml`, run 37881474608, 2026-10-09): 1.167 pruebas generales + 117 de PostgreSQL, con las guardas de CI sin omisiones ni exclusiones. Las métricas actuales, medidas por separado, son **90 archivos de prueba**, **1.252 definiciones AST** de funciones/métodos `test_*` y **1.284 ítems recopilados por pytest** (`pytest tests/ tests_pg/ --collect-only -q` con `TURNOBOT_PG_URL`). El run verificado anterior (commit `c60ac24e`) fue **1.252 passed / 0 failed**, antes de los +32 items del runner PG (`database/pg_migrator.py`) —`tests/test_pg_migrator.py` (20) y `tests_pg/test_pg_migrator_live.py` (12)— (ver `docs/TEST_SUITE_INVENTORY.md`).

Los 2 fallos de symlink en Windows y el flakiness de concurrencia listados abajo siguen siendo esperados como fallos preexistentes.

## Note on Flaky Windows Tests

On Windows, two categories of tests are known to fail intermittently due to platform limitations:

1. **Symlink tests** — Windows does not support symlinks without elevated privileges:
   - `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json` (may fail when symlinks not available)
   - `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted`

2. **Concurrency tests** — threading/transaction timing differences on Windows:
   - `tests/test_security_concurrency.py::AtomicSecurityTests::test_email_claim_allows_one_worker_and_retry_after_failure`

The count of passing/failing tests may vary between runs depending on Windows environment state, but the total remains stable.

## Blueprint Migration Notes

- The `name=''` parameter in `app.register_blueprint()` is reserved for `auth_bp` (Paso 4)
- The `/health` endpoint is registered via `app.add_url_rule()` to avoid consuming `name=''`
- The `/` and `/b/<slug>` routes are registered via `app.add_url_rule()` to preserve global endpoint names (`index`, `business_index`)
- `routes/public.py` = módulo de rutas (NOT a registered Blueprint)
- `routes/health.py` = módulo de ruta (Blueprint definido pero NO registrado)
- Blueprint registration is handled in `routes/__init__.py:register_blueprints(app)`

## Operaciones y Servidores (AWS / OCI)

Instrucciones permanentes para agentes de IA (Kilo Code, OpenCode, Antigravity y otros):

- **Inventario de Servidores**: Consultar obligatoriamente `docs/operations/SERVERS.md` antes de realizar cualquier tarea relacionada con servidores, red o infraestructura.
- **Documentación Operativa**: Consultar la documentación real de despliegue (`DEPLOYMENT.md`) y backups/cutover (`CUTOVER_PROCEDURE.md`) antes de operar.
- **Distinción de Entornos**: Distinguir siempre entre **AWS EC2** (servidor actual de producción) y **OCI** (servidor anterior que puede mantener timers auxiliares).
- **Diagnóstico Previo**: Verificar IP, conectividad TCP (puerto 22), autenticación SSH y estado de servicios antes de intervenir o diagnosticar un servidor.
- **Confidencialidad de Secretos**: No revelar jamás claves privadas (`.pem` / `.key`), contraseñas, tokens de sesión ni secretos SMTP en respuestas, logs, prompts o commits.
- **Acciones Críticas**: No detener servicios (`turnobot.service`), no modificar producción, no ejecutar migraciones de base de datos ni eliminar backups sin autorización explícita del usuario.
- **Notificaciones**: No enviar notificaciones reales (correos vía SMTP o mensajes) durante pruebas o diagnósticos.
- **Control de Versiones**: No ejecutar `git commit`, `git push`, `git merge` ni despliegues a servidores sin autorización explícita.
- **Restauración de Base de Datos**: Usar siempre una base de datos aislada y descartable para cualquier prueba de restauración (`restore_postgresql.py`).
- **Claridad de Informes**: Informar siempre con precisión qué se verificó, el estado observable y qué puntos quedan pendientes de confirmación.
