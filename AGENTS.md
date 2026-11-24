# AGENTS.md â€” Project Guidelines

## Lint & Typecheck

- Python lint: `python -m ruff check .` (if ruff is installed)
- Typecheck: `python -m mypy app.py` (if mypy is installed)

## Test

- Run tests: `python -m pytest tests/ tests_pg/ -v --tb=short`
  (la suite `tests_pg/` y los tests funcionales PG se omiten si `TURNOBOT_PG_URL`
  no estÃ¡ definida)
- Coverage is enabled by default via pyproject.toml configuration

## Pre-existing Test Failures (Baseline)

The following tests fail on Windows due to platform limitations, unrelated to feature work:

1. `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json`
   - Failure cause: Windows does not support symlinks without elevated privileges
   - This test relies on symlink behavior for appointment rescheduling

2. `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted`
   - Failure cause: Windows symlink limitations (same as above)
   - This test validates backup symlink preservation during prune operations

**Expected baseline:** el run completo verificado mÃ¡s reciente (CI) es **1.284 passed / 0 failed / 0 skipped / 0 errores** sobre el commit `c3fee3a` (`feat: add versioned PostgreSQL migration runner`, workflow `tests.yml`, run 37881474608, 2026-10-09): 1.167 pruebas generales + 117 de PostgreSQL, con las guardas de CI sin omisiones ni exclusiones. Las mÃ©tricas actuales, medidas por separado, son **90 archivos de prueba**, **1.252 definiciones AST** de funciones/mÃ©todos `test_*` y **1.284 Ã­tems recopilados por pytest** (`pytest tests/ tests_pg/ --collect-only -q` con `TURNOBOT_PG_URL`). El run verificado anterior (commit `c60ac24e`) fue **1.252 passed / 0 failed**, antes de los +32 items del runner PG (`database/pg_migrator.py`) â€”`tests/test_pg_migrator.py` (20) y `tests_pg/test_pg_migrator_live.py` (12)â€” (ver `docs/TEST_SUITE_INVENTORY.md`).

Los 2 fallos de symlink en Windows y el flakiness de concurrencia listados abajo siguen siendo esperados como fallos preexistentes.

## Note on Flaky Windows Tests

On Windows, two categories of tests are known to fail intermittently due to platform limitations:

1. **Symlink tests** â€” Windows does not support symlinks without elevated privileges:
   - `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json` (may fail when symlinks not available)
   - `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted`

2. **Concurrency tests** â€” threading/transaction timing differences on Windows:
   - `tests/test_security_concurrency.py::AtomicSecurityTests::test_email_claim_allows_one_worker_and_retry_after_failure`

The count of passing/failing tests may vary between runs depending on Windows environment state, but the total remains stable.

## Blueprint Migration Notes

- The `name=''` parameter in `app.register_blueprint()` is reserved for `auth_bp` (Paso 4)
- The `/health` endpoint is registered via `app.add_url_rule()` to avoid consuming `name=''`
- The `/` and `/b/<slug>` routes are registered via `app.add_url_rule()` to preserve global endpoint names (`index`, `business_index`)
- `routes/public.py` = mÃ³dulo de rutas (NOT a registered Blueprint)
- `routes/health.py` = mÃ³dulo de ruta (Blueprint definido pero NO registrado)
- Blueprint registration is handled in `routes/__init__.py:register_blueprints(app)`

## Operaciones y Servidores (AWS / OCI)

Instrucciones permanentes para agentes de IA (Kilo Code, OpenCode, Antigravity y otros):

- **Inventario de Servidores**: Consultar obligatoriamente `docs/operations/SERVERS.md` antes de realizar cualquier tarea relacionada con servidores, red o infraestructura.
- **DocumentaciÃ³n Operativa**: Consultar la documentaciÃ³n real de despliegue (`DEPLOYMENT.md`) y backups/cutover (`CUTOVER_PROCEDURE.md`) antes de operar.
- **DistinciÃ³n de Entornos**: Distinguir siempre entre **AWS EC2** (servidor actual de producciÃ³n) y **OCI** (servidor anterior que puede mantener timers auxiliares).
- **DiagnÃ³stico Previo**: Verificar IP, conectividad TCP (puerto 22), autenticaciÃ³n SSH y estado de servicios antes de intervenir o diagnosticar un servidor.
- **Confidencialidad de Secretos**: No revelar jamÃ¡s claves privadas (`.pem` / `.key`), contraseÃ±as, tokens de sesiÃ³n ni secretos SMTP en respuestas, logs, prompts o commits.
- **Acciones CrÃ­ticas**: No detener servicios (`turnobot.service`), no modificar producciÃ³n, no ejecutar migraciones de base de datos ni eliminar backups sin autorizaciÃ³n explÃ­cita del usuario.
- **Notificaciones**: No enviar notificaciones reales (correos vÃ­a SMTP o mensajes) durante pruebas o diagnÃ³sticos.
- **Control de Versiones**: No ejecutar `git commit`, `git push`, `git merge` ni despliegues a servidores sin autorizaciÃ³n explÃ­cita.
- **RestauraciÃ³n de Base de Datos**: Usar siempre una base de datos aislada y descartable para cualquier prueba de restauraciÃ³n (`restore_postgresql.py`).
- **Claridad de Informes**: Informar siempre con precisiÃ³n quÃ© se verificÃ³, el estado observable y quÃ© puntos quedan pendientes de confirmaciÃ³n.

### PolÃ­tica obligatoria de validaciÃ³n y pruebas

**1. Fuente de verdad para la validaciÃ³n integral**
- La validaciÃ³n integral del proyecto debe realizarse mediante GitHub Actions.
- La ejecuciÃ³n debe incluir `tests/` y `tests_pg/`, con PostgreSQL y Python 3.12 en Linux.
- Una ejecuciÃ³n exitosa de los grupos aislados no equivale a una ejecuciÃ³n exitosa de la suite completa.

**2. Uso de Windows**
- Windows se utiliza principalmente para desarrollo y validaciones puntuales.
- Se pueden ejecutar Ruff, Ruff Format, MyPy y grupos especÃ­ficos de pruebas.
- Si la suite completa presenta bloqueos relacionados con conexiones TCP, acumulaciÃ³n de conexiones, puertos o limitaciones de symlinks, no insistir repetidamente con ejecuciones completas en Windows.
- En esos casos, utilizar GitHub Actions en Linux para la validaciÃ³n integral.

**3. No sumar resultados parciales**
- Nunca sumar resultados de diferentes ejecuciones parciales para afirmar que pasÃ³ la suite completa.
- Informar claramente quÃ© grupos se ejecutaron, cuÃ¡les pasaron, cuÃ¡les fallaron y cuÃ¡les fueron omitidos.
- Solo declarar la suite integral aprobada cuando exista una ejecuciÃ³n integral exitosa en GitHub Actions.

**4. VerificaciÃ³n de MyPy**
- MyPy debe comprobar explÃ­citamente los archivos Python modificados que corresponda validar.
- Antes de declarar MyPy aprobado, verificar quÃ© archivos incluye realmente la configuraciÃ³n.
- Una ejecuciÃ³n exitosa que no incluya los archivos modificados no demuestra que esos archivos estÃ©n correctamente tipados.
- Si existen errores, informar archivo, lÃ­nea y mensaje exacto.

**5. Evidencia obligatoria en los informes**
Todo informe de validaciÃ³n debe identificar:
- Rama y SHA exacto del commit probado.
- Enlace a la ejecuciÃ³n correspondiente de GitHub Actions.
- Cantidad de pruebas aprobadas, fallidas, omitidas y errores.
- Resultado de Ruff, formato y MyPy, indicando su alcance real.
- Limitaciones o validaciones que no se pudieron completar.

**6. Honestidad en los resultados**
- No declarar una validaciÃ³n aprobada si no se ejecutÃ³.
- No presentar resultados parciales como resultados integrales.
- No confundir una ejecuciÃ³n local exitosa con una validaciÃ³n completa en CI.
- No ocultar fallos, errores, skips ni limitaciones.
