# AGENTS.md — Project Guidelines

## Lint & Typecheck

- Python lint: `python -m ruff check .` (if ruff is installed)
- Typecheck: `python -m mypy` (usa `[tool.mypy].files`, que incluye explícitamente los archivos Python modificados)

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

**Expected baseline:** el run integral verificado más reciente en CI es el workflow `tests.yml`, run `45396217542`, sobre `2e2128bbff74ed8ba66652512d38fb032d4d099a`: job `test` con Ruff check/format OK, MyPy incremental OK, **1.817 passed / 2 skipped** en `tests/`, **145 passed** en `tests_pg/`, 35 subtests passed y T4-T6 3 passed; jobs `visual-tests` y `sonarqube_analysis` también en success. Esa evidencia es anterior a los cambios locales actualmente en esta rama. El run integral verificado en CI para los cambios actuales es **GitHub Actions #218** sobre el commit `4720b374b7c40c8e7c51326c97e8697ba3363669` (rama `recovery/local-improvements-20261124`): job `test` con Ruff check/format OK, MyPy OK, tests en verde. Esa evidencia **sí incluye** los cambios de esta revisión.

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

### Política obligatoria de validación y pruebas

**1. Fuente de verdad para la validación integral**
- La validación integral del proyecto debe realizarse mediante GitHub Actions.
- La ejecución debe incluir `tests/` y `tests_pg/`, con PostgreSQL y Python 3.12 en Linux.
- Una ejecución exitosa de los grupos aislados no equivale a una ejecución exitosa de la suite completa.

**2. Uso de Windows**
- Windows se utiliza principalmente para desarrollo y validaciones puntuales.
- Se pueden ejecutar Ruff, Ruff Format, MyPy y grupos específicos de pruebas.
- Si la suite completa presenta bloqueos relacionados con conexiones TCP, acumulación de conexiones, puertos o limitaciones de symlinks, no insistir repetidamente con ejecuciones completas en Windows.
- En esos casos, utilizar GitHub Actions en Linux para la validación integral.

**3. No sumar resultados parciales**
- Nunca sumar resultados de diferentes ejecuciones parciales para afirmar que pasó la suite completa.
- Informar claramente qué grupos se ejecutaron, cuáles pasaron, cuáles fallaron y cuáles fueron omitidos.
- Solo declarar la suite integral aprobada cuando exista una ejecución integral exitosa en GitHub Actions.

**4. Verificación de MyPy**
- MyPy debe comprobar explícitamente los archivos Python modificados que corresponda validar.
- Antes de declarar MyPy aprobado, verificar qué archivos incluye realmente la configuración.
- Una ejecución exitosa que no incluya los archivos modificados no demuestra que esos archivos estén correctamente tipados.
- Si existen errores, informar archivo, línea y mensaje exacto.

**5. Evidencia obligatoria en los informes**
Todo informe de validación debe identificar:
- Rama y SHA exacto del commit probado.
- Enlace a la ejecución correspondiente de GitHub Actions.
- Cantidad de pruebas aprobadas, fallidas, omitidas y errores.
- Resultado de Ruff, formato y MyPy, indicando su alcance real.
- Limitaciones o validaciones que no se pudieron completar.

**6. Honestidad en los resultados**
- No declarar una validación aprobada si no se ejecutó.
- No presentar resultados parciales como resultados integrales.
- No confundir una ejecución local exitosa con una validación completa en CI.
- No ocultar fallos, errores, skips ni limitaciones.
