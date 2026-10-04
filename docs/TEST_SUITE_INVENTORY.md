# Inventario canónico de tests después del cutover PostgreSQL

Estado auditado: revisión posterior a la migración de `test_password_reset.py`.

Este documento clasifica cada archivo `test_*.py` de `tests/` y `tests_pg/` exactamente una vez. Las cifras se verificaron contra una **colección real** (`pytest tests/ tests_pg/ --collect-only -q` con `TURNOBOT_PG_URL` configurada), que arroja **81 archivos y 1.089 items**, y de forma independiente contra un conteo **AST** de funciones y métodos `test_*`, que coincide archivo por archivo salvo el caso parametrizado conocido.

Histórico de discrepancias detectadas y corregidas contra la realidad:

- `tests/test_appointment_concurrency_extra.py` (6 tests) eliminado; su contenido quedó absorbido en `tests/test_appointments.py`, que pasó de 38 a 44 tests.
- `tests/test_business_context_pg.py` (10 tests) eliminado al consolidarse con `tests/test_business_context.py`; la categoría D queda con un solo archivo.
- `tests_pg/test_concurrency.py` tiene 7 tests, no 9.
- `test_chat_recovery.py` (6), `test_branding.py` (8), `test_provisioning.py` (2) y `test_reminders.py` (3) estaban aún en A pese a estar migrados; se movieron a B.
- 13 archivos que seguían erróneamente en A pese a estar migrados y validados contra PostgreSQL se movieron a B en esta revisión: `test_ai_model`, `test_business_settings`, `test_knowledge`, `test_membership_hardening`, `test_membership_http`, `test_membership_management`, `test_notifications`, `test_notifications_conditional`, `test_onboarding`, `test_security_concurrency`, `test_security_operations`, `test_services_isolation`, `test_staff_invitation` (13 archivos / 111 tests).
- `test_connection_usage.py` salió de A y pasó a **E**: no es una migración de boilerplate sino una **deuda de rediseño** (ver E).
- `test_membership_ui.py` (13) y `test_resources_appointments.py` (13) se movieron de A a B al cerrarse su migración a PostgreSQL (2 archivos / 26 tests).
- `test_password_reset.py` (13) se movió de A a B al cerrarse su migración: flujo de recuperación de contraseña sobre PG, con token de un solo uso, rechazo de contraseña débil, contrato anti-enumeración (email desconocido → `success=True`, `token=None`) y revocación de las sesiones del usuario verificada sobre el servidor. FASE 3 acumulada: **123/123 PASS** sobre 11 bloques cerrados.
- El total de items subió de 1.077 a **1.089** por los 12 tests nuevos del adapter añadidos en la corrección de `_adapt_boolean_comparisons` (`tests/test_fase_4f_query_adaptation.py`: 25 → 37). Las dos migraciones posteriores no alteraron el número total de tests.

`tests/test_cli_pg_pool_lifecycle.py` declara 9 funciones de test y una está parametrizada con dos valores, por lo que aporta 10 items: es la única diferencia entre AST (9) y colección (10).

## A — Migrar/normalizar al patrón PostgreSQL

Son pruebas funcionales o de persistencia que necesitan la base temporal PostgreSQL y cuya preparación/assertions aún arrastran una base temporal SQLite, un helper SQLite o una base de test heredada. “Migrar” aquí significa conservar el comportamiento que prueban y retirar la dependencia SQLite del test; no implica que haya un error de producto.

| Archivo | Tests | Categoría | Motivo | Próxima acción |
| --- | ---: | --- | --- | --- |
| `tests/test_ai_tools_integration.py` | 14 | A | Integración de herramientas con persistencia. | Migrar preparación y asserts a PG. |
| `tests/test_conversations.py` | 18 | A | Sesiones y mensajes persistidos. | Migrar setup/consultas a PG. |
| `tests/test_emails_etapa4.py` | 12 | A | Flujo de email con estado de aplicación. | Usar la base temporal PG. |
| `tests/test_membership_authorization.py` | 22 | A | Autorización con datos/relaciones persistidas. | Usar seed y app PG. |
| `tests/test_multi_tenant_admin.py` | 18 | A | Administración y aislamiento entre tenants. | Seed de varios negocios en PG. |
| `tests/test_observability.py` | 79 | A | Cobertura extensa; incluye estado DB y mocks de locks SQLite. | Auditar por bloques; mantener mocks unitarios útiles y pasar escenarios funcionales a PG. |
| `tests/test_platform.py` | 18 | A | Estado funcional de plataforma/admin. | Usar DB temporal PG. |
| `tests/test_provision_business.py` | 21 | A | Provisionamiento con múltiples registros relacionados. | Migrar datos iniciales a PG. |
| `tests/test_public_api_service_isolation.py` | 84 | A | Pruebas grandes de API/aislamiento con persistencia. | Auditar en bloques y usar seeds PG por tenant. |
| `tests/test_rate_limit_http.py` | 4 | A | Límites HTTP con cliente/app y estado de aplicación. | Separar estado en memoria de DB y usar PG donde corresponda. |
| `tests/test_rate_limiting.py` | 8 | A | Incluye `last_insert_rowid()`/errores SQLite en escenarios funcionales. | Migrar inserciones/assertions a resultados PG. |
| `tests/test_resources_public_api.py` | 28 | A | API pública con recursos y DB. | Migrar preparación/assertions a PG. |
| `tests/test_standalone_create_app.py` | 4 | A | El subproceso fuerza `DB_BACKEND=sqlite`; prueba arranque/app funcional. | Pasar una URL temporal PG al subproceso o aislar el contrato de factory sin DB. |

**13 archivos / 330 tests.**

## B — PostgreSQL ya cubierto

Incluye integración live en `tests_pg/`, funcionalidad ya ejecutada mediante el pool PostgreSQL del harness común y contratos/unit tests específicos de la capa PostgreSQL. No se vuelve a incluir como migración pendiente solo por conservar nombres/helpers legacy.

| Archivo | Tests | Categoría | Motivo | Próxima acción |
| --- | ---: | --- | --- | --- |
| `tests/test_accessibility_ui.py` | 21 | B | App/DB funcional bajo aislamiento PostgreSQL común. | Mantener; ajustar solo si aparece dependencia SQLite real. |
| `tests/test_admin_actions_coverage.py` | 11 | B | Cobertura admin respaldada por app/DB PostgreSQL. | Mantener como cobertura PG. |
| `tests/test_admin_business_settings.py` | 3 | B | Settings del panel sobre la app aislada en PG. | Mantener como cobertura PG. |
| `tests/test_admin_panel.py` | 33 | B | Las requests usan el pool temporal PostgreSQL de `pg_isolate_module_app`; `DATABASE_PATH` no selecciona el backend. | Mantener. Refactor interno pendiente: normalizar posteriormente al patrón `PostgreSQLTestCase` si aporta simplificación. |
| `tests/test_admin_services.py` | 7 | B | Servicios del panel sobre app/DB PostgreSQL común. | Mantener como cobertura PG. |
| `tests/test_ai_model.py` | 6 | B | Migrado: preparación sobre la base PostgreSQL por test del harness, sin `DATABASE_PATH`/`init_database()`. | Mantener como cobertura PG. |
| `tests/test_appointments.py` | 44 | B | Usa `PostgreSQLTestCase` y DB temporal por test; absorbió los 6 casos de concurrencia que vivían en el archivo `test_appointment_concurrency_extra.py` eliminado. | Mantener como canónica para reservas. |
| `tests/test_appointment_intervals.py` | 16 | B | Intervalos y disponibilidad de turnos sobre DB temporal PostgreSQL; usa `slot_duration` del seed y reinicia la secuencia `services.id`. | Mantener como cobertura PG. |
| `tests/test_appointments_isolation.py` | 15 | B | Aislamiento de turnos por tenant bajo harness PostgreSQL. | Mantener como cobertura PG. |
| `tests/test_app_security.py` | 6 | B | No abre SQLite; usa cliente de la app global aislada por PG. Requiere app/cliente y seed estándar para hooks de tenant. | Mantener; fixture explícito sería una mejora de claridad, no una migración de backend. |
| `tests/test_business_settings.py` | 4 | B | Migrado: settings del negocio sobre la base PostgreSQL por test. | Mantener como cobertura PG. |
| `tests/test_branding.py` | 8 | B | Branding persistido por negocio sobre PostgreSQL live: validación HEX/URL de logo y persistencia en `business_settings`. | Mantener como cobertura PG. |
| `tests/test_chat_recovery.py` | 6 | B | Recuperación de sesión de chat e historial público por token, sobre PostgreSQL live. | Mantener como cobertura PG. |
| `tests/test_cli_pg_pool_lifecycle.py` | 10 | B | Contrato del lifecycle PostgreSQL; 9 funciones, una parametrizada en dos casos. | Mantener; no precisa DB live para los mocks del CLI. |
| `tests/test_fase_4d_lifecycle.py` | 8 | B | Lifecycle de la app/pool PostgreSQL. | Mantener. |
| `tests/test_knowledge.py` | 8 | B | Migrado: búsqueda/gestión de conocimiento sobre PG; contrastado con `tests_pg/test_knowledge_search_live.py` sin pérdida de assertions. | Mantener como cobertura PG. |
| `tests/test_load_current_business_regression.py` | 8 | B | Requests de tenant sobre app PG común; `/b/el-corte/...` necesita seed estándar. | Mantener; podría usar explícitamente `client`/`seed`. |
| `tests/test_loyalty_10_2_10_3.py` | 8 | B | Rewards, redenciones y retention sobre PostgreSQL live; extiende `LoyaltyBase` migrada. | Mantener como cobertura PG funcional. |
| `tests/test_loyalty.py` | 34 | B | Loyalty, ledger, redenciones y ajustes sobre PostgreSQL live; `LoyaltyBase` sin preparación SQLite y los 4 grupos (14+9+6+5) verificados en PG. | Mantener como canónica de fidelización. |
| `tests/test_membership_hardening.py` | 10 | B | Migrado: casos 14/15/16/19 (owner único, sesión tras revocación, rol reflejado en la siguiente petición, no-escalada vía `role_id`/`owner_id`/`business_id`) sobre PG; `expires_at` verificado como `TIMESTAMPTZ` con timezone. | Mantener como cobertura de seguridad. |
| `tests/test_membership_http.py` | 21 | B | Migrado: capa HTTP de membresía (21 tests, no 22) con login, sesión, CSRF y política de roles; el tenant se sigue resolviendo por slug. | Mantener como canónica HTTP de membresía. |
| `tests/test_membership_management.py` | 9 | B | Migrado: helpers `*_scoped` de membresía sobre PG; la integridad FK de `role_id` inválido la rechaza el servidor. | Mantener como canónica de la capa de helpers. |
| `tests/test_membership_ui.py` | 13 | B | Migrado: UI real de `templates/usuarios.html` sobre PG — visibilidad por rol, CSRF en los 3 formularios, ausencia de `role_id`/`business_id` como campos y rutas con prefijo `/b/<slug>/`. | Mantener como canónica de la UI de membresía. |
| `tests/test_notifications.py` | 8 | B | Migrado: persistencia y entrega de notificaciones sobre PG. | Mantener como cobertura PG. |
| `tests/test_notifications_conditional.py` | 7 | B | Migrado: notificaciones condicionadas por estado persistido en PG. | Mantener como cobertura PG. |
| `tests/test_notifications_hardening.py` | 18 | B | Migrado: usa `PostgreSQLTestCase` con base desechable por test; mantiene `retry_failed_notifications._run_once()` real con su propio pool de CLI, y conserva `SET notifications_enabled = 1` para cubrir de punta a punta la adaptación booleana del seam PG. | Mantener como cobertura PG. |
| `tests/test_onboarding.py` | 7 | B | Migrado: estado de onboarding persistido en PG. | Mantener como cobertura PG. |
| `tests/test_password_reset.py` | 13 | B | Migrado: recuperación de contraseña sobre PG — token de un solo uso, contraseña débil rechazada, anti-enumeración de emails y revocación de las sesiones del usuario tras el reset. | Mantener como canónica de recuperación de contraseña. |
| `tests/test_postgres_config.py` | 12 | B | Resolver/configuración PostgreSQL; el rechazo de SQLite en producción sigue siendo contrato vigente. | Mantener como unit tests de configuración. |
| `tests/test_postgres_schema.py` | 17 | B | Inspecciona `migrations_pg/001_initial_schema.sql`; las menciones SQLite son assertions negativas del esquema PostgreSQL. | Mantener como validación estática PG. |
| `tests/test_postgresql_backup_scripts.py` | 4 | B | Prueba builders/sanitización/comandos de backup PostgreSQL; no hace `sqlite3.connect` ni ejecuta SQLite. | Mantener; no hay cobertura equivalente en otro archivo. |
| `tests/test_provisioning.py` | 2 | B | Provisionamiento atómico (negocio + owner + settings + 7 horarios), unicidad de slug y rollback por email duplicado, sobre PostgreSQL live. | Mantener como cobertura PG. |
| `tests/test_registro_publico.py` | 14 | B | Migrado: registro público sobre PG — flujo de invitación, entrega de email y rate limiting, con `client` del harness. | Mantener como cobertura PG. |
| `tests/test_reminders.py` | 3 | B | Runner de recordatorios 24h sobre PostgreSQL live: envío, idempotencia y supresión con notificaciones deshabilitadas o sin email. `_run_once()` resuelve el pool desde `DATABASE_URL`, alineado en el test con la base temporal por test. | Mantener como cobertura PG. |
| `tests/test_resources_appointments.py` | 13 | B | Migrado: recursos y turnos persistidos sobre PG; el aislamiento por tenant es el de `business_id`, sin swap de `DATABASE_PATH`. | Mantener como cobertura PG. |
| `tests/test_resources_isolation.py` | 13 | B | Migrado: aislamiento de recursos entre negocios A y B sobre la base temporal que el harness descarta por test. | Mantener como cobertura PG. |
| `tests/test_security_concurrency.py` | 3 | B | Migrado: ejecución contra PG real de los escenarios de concurrencia de seguridad. | Mantener como cobertura PG. |
| `tests/test_security_operations.py` | 9 | B | Migrado: operaciones de seguridad y persistencia sobre PG. | Mantener como cobertura PG. |
| `tests/test_services_isolation.py` | 9 | B | Migrado: aislamiento de servicios por tenant con seed PG por negocio. | Mantener como cobertura PG. |
| `tests/test_staff_invitation.py` | 10 | B | Migrado: ciclo de invitación por token (`provision → approve → accept`, un solo uso, `used_at`) y frontera de autorización `admin → forbidden` sobre PG. | Mantener como cobertura PG. |
| `tests/test_turnogo_landing_wizard.py` | 32 | B | Rutas funcionales usan app global PG, negocio/servicios/horarios del seed estándar. | Mantener; podría explicitar `client` y seed. |
| `tests/test_stage11_product.py` | 3 | B | Resumen de producto y onboarding sobre PostgreSQL live; hereda `LoyaltyBase` migrada. | Mantener como cobertura PG funcional. |
| `tests_pg/test_advisory_lock_live.py` | 2 | B | Locks consultivos contra PostgreSQL live. | Mantener en integración PG. |
| `tests_pg/test_concurrency.py` | 7 | B | Concurrencia PostgreSQL live. | Mantener en integración PG. |
| `tests_pg/test_knowledge_search_live.py` | 12 | B | Búsqueda PostgreSQL live. | Mantener; la cobertura equivalente ya está en `tests/test_knowledge.py` (B). |
| `tests_pg/test_migrator.py` | 15 | B | PostgreSQL es el destino/objeto de integración; crea SQLite local solo como origen controlado de `migrator.py`. | Mantener como integración PG de migración; documentar explícitamente su origen SQLite. |
| `tests_pg/test_pool_live.py` | 7 | B | Pool/conexiones/transacciones PostgreSQL live. | Mantener en integración PG. |
| `tests_pg/test_reschedule_admin_lock_live.py` | 7 | B | Reprogramación y lock contra PostgreSQL live. | Mantener en integración PG. |
| `tests_pg/test_schema_live.py` | 14 | B | Aplica/verifica schema PostgreSQL live. | Mantener; es complementaria a la validación estática. |
| `tests_pg/test_seed_sequences.py` | 8 | B | Cobertura del harness: `sync_identity_sequences()` sincroniza las secuencias IDENTITY tanto en bases solo-schema como schema+seed. | Mantener como contrato del seed. |
| `tests_pg/test_session_live.py` | 12 | B | Session, app factory y errores sobre pool PostgreSQL live. | Mantener en integración PG. |

**48 archivos / 545 tests.**

## C — Mantener SQLite por razón técnica vigente

El objeto es el runner/migración de esquema legacy SQLite o una herramienta operativa SQLite aún referenciada por despliegue. No convertir estos tests a PostgreSQL: hacerlo borraría precisamente el contrato legacy que aún se consume.

| Archivo | Tests | Categoría | Motivo | Próxima acción |
| --- | ---: | --- | --- | --- |
| `tests/test_appointment_idempotency_migration.py` | 2 | C | Valida la migración SQLite del índice/idempotencia para bases legacy. | Mantener aislado como migración SQLite. |
| `tests/test_appointment_intervals_migration.py` | 1 | C | Valida upgrade de esquema SQLite de intervalos. | Mantener aislado como migración SQLite. |
| `tests/test_business_migration.py` | 3 | C | Valida las migraciones SQLite antiguas de negocio/tenant y preservación de datos. | Mantener como compatibilidad del origen del migrator. |
| `tests/test_migrations_audit.py` | 4 | C | Valida `schema_version`/`migration_log` del runner SQLite. | Mantener mientras el runner sea consumidor del formato legacy. |
| `tests/test_backup_database.py` | 5 | C | Prueba backup SQLite online/WAL mediante `sqlite3.Connection.backup`. | Mantener mientras exista el script/timer legacy. |
| `tests/test_prune_backups.py` | 11 | C | Prueba retención y seguridad de archivos `.db`; el timer systemd sigue ejecutando el script. | Mantener; el test de symlink conserva limitación Windows sin privilegios. |
| `tests/test_verify_backup.py` | 10 | C | Prueba verificación de integridad/restore de backups SQLite mediante PRAGMA. | Mantener mientras exista la herramienta legacy. |

**7 archivos / 36 tests.**

## D — Consolidación resuelta

| Archivo | Tests | Categoría | Motivo | Próxima acción |
| --- | ---: | --- | --- | --- |
| `tests/test_business_context.py` | 10 | D | Se mantiene como variante canónica tras eliminar `tests/test_business_context_pg.py` (10 tests duplicados). | Conservar; ya no hay par que consolidar. |

**1 archivo / 10 tests.**

La consolidación propuesta en revisiones anteriores está resuelta: la variante `_pg` se eliminó y sus 10 escenarios quedaron cubiertos por `test_business_context.py`, que usa la fachada `application.app` y el harness PostgreSQL común. No queda duplicado pendiente.

## E — Revisión manual / unit tests desacoplables

| Archivo | Tests | Categoría | Motivo | Próxima acción |
| --- | ---: | --- | --- | --- |
| `tests/test_application_factory.py` | 6 | E | Contratos de factory/URL map/hooks y guardas; fuerza SQLite para evitar infraestructura. | Separar estructura/config de inicialización DB y usar doubles PG donde aplique; no convertirlo mecánicamente en integración. |
| `tests/test_check_health.py` | 13 | E | Prueba el script HTTP con servidor loopback stdlib; no importa app ni abre DB. | Desacoplar del fixture PostgreSQL y mantener como unit test. |
| `tests/test_connection_usage.py` | 3 | E | **Deuda de rediseño, no migración.** Mide aperturas parcheando `database.database.sqlite3.connect`, una función que el backend PostgreSQL nunca ejecuta: por eso sus 3 tests fallan con `0 != 1` en PG. Su intención (contar checkouts del pool) es válida, pero requiere otro mecanismo de medición. | Rediseñar la medición sobre el pool PG (`getconn`/`putconn`) como tarea propia; **no** incluir en la migración normal de FASE 3. |
| `tests/test_fase_4f_query_adaptation.py` | 37 | E | Unit tests de traducción SQL/PRAGMA/`BEGIN IMMEDIATE`, más 4 casos que ejecutan SQL **real** contra PostgreSQL para los literales booleanos por contexto (`SET` vs predicado). | Mantener como tests del adapter mientras ese contrato de compatibilidad exista; retirar subcasos solo en una fase de retiro del adapter. |
| `tests/test_fase_4g_transaction_isolation.py` | 21 | E | Archivo mixto: 17 tests de traducción/transacciones/advisory lock con mocks; 4 pruebas funcionales de loyalty/session usan SQLite; 1 de los 17 valida el no-op SQLite. | Separar conceptualmente: migrar 4 casos funcionales a PG; conservar unit tests del adapter; reevaluar el caso SQLite cuando se retire ese backend. |
| `tests/test_json_provider.py` | 7 | E | Prueba serialización JSON sin persistencia. | Ejecutar sin app/DB fixture si imports lo permiten. |
| `tests/test_pg_pool_hardening.py` | 21 | E | Mayormente fake pool/proxy; 3 casos ejercitan explícitamente el backend SQLite. No necesita PostgreSQL live. | `NO MIGRAR` como integración: conservar unit tests PG del adapter; separar/revisar los 3 casos SQLite antes de retirarlos. |
| `tests/test_rate_limit_memory.py` | 8 | E | Lógica de rate limit en memoria. | Desacoplar de fixture/app DB. |
| `tests/test_rate_limiting_phone.py` | 7 | E | Contadores telefónicos en memoria y logging; no necesita persistencia. | Desacoplar de fixture/app DB. |

**9 archivos / 123 tests.**

### Desglose interno solicitado

- `test_connection_usage.py`: clasificado E y **fuera de la cola de migración de FASE 3**. Sus 3 tests parchean `sqlite3.connect`, que el backend PostgreSQL no invoca, así que no es un archivo de boilerplate SQLite retirable: el objeto mismo del test (la métrica) hay que redefinirlo sobre el pool. Sus 3 tests fallan en la suite completa con `AssertionError: 0 != 1`, y esa cifra es la causa de raíz, no una regresión.
- `test_fase_4f_query_adaptation.py`: 25 tests unitarios previos (conservados) + 12 añadidos con la corrección de `_adapt_boolean_comparisons`: 8 comparan el SQL adaptado por contexto y 4 ejecutan la sentencia contra un PostgreSQL real a través de `PgConnectionProxy` (no `MagicMock`). El caso `MagicMock` previo (`test_pg_connection_proxy_execution_flow`) no detectaba el bug porque nunca parsea la sentencia contra un servidor.
- `test_fase_4g_transaction_isolation.py`: 3 traducciones de errores y 10 casos de comandos/transacciones son unit tests del adapter (13); 3 guardas de loyalty y 1 carrera de `get_or_create` son funcionalidad DB que debe ir a PostgreSQL (4); `test_advisory_lock_noop_en_sqlite` es el único caso cuyo objeto explícito es el comportamiento SQLite (1). El archivo queda E hasta separar esos niveles.
- `test_pg_pool_hardening.py`: prueba principalmente selección de pool, `PgConnectionProxy`, sesión, teardown y `create_app` con pools falsos. Tres tests seleccionan/ejercitan SQLite; el resto no requiere servidor live. No migrar el bloque unitario a una DB real.
- `test_turnogo_landing_wizard.py`: no usa SQLite directamente; sí necesita app/client sobre PostgreSQL y seed estándar (negocio, servicios, horarios) para los escenarios funcionales.
- `test_app_security.py`: no usa SQLite directamente; necesita app/client. El hook de tenant y las rutas bajo prueba dependen de que el seed estándar PG exista.
- `test_business_context.py`: sin SQLite directo, necesita app y negocio seed; se conserva en D como variante canónica tras eliminarse su duplicado `_pg`.

## Totales canónicos

Los items por archivo cuentan la expansión parametrizada indicada arriba.

| Categoría | Archivos | Tests/items |
| --- | ---: | ---: |
| A — Migrar/normalizar a PostgreSQL | 13 | 330 |
| B — PostgreSQL ya cubierto | 51 | 590 |
| C — Mantener SQLite legítimo | 7 | 36 |
| D — Consolidación resuelta | 1 | 10 |
| E — Revisión manual/unit desacoplable | 9 | 123 |
| **TOTAL** | **81** | **1.089** |

Comprobación: `13 + 51 + 7 + 1 + 9 = 81`; `330 + 590 + 36 + 10 + 123 = 1.089`.

## Adaptador de compatibilidad: corrección de `_adapt_boolean_comparisons`

`database/pg_pool.py` convertsía los literales booleanos `1/0` de una columna `BOOLEAN` aplicando la forma de **predicado** a la sentencia completa, incluida la lista de asignaciones de un `UPDATE`. Eso generaba SQL inválido:

```sql
UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0
-- antes:  UPDATE sessions SET revoked IS TRUE WHERE user_id = %s AND NOT revoked
-- ahora:  UPDATE sessions SET revoked = TRUE WHERE user_id = %s AND NOT revoked
```

PostgreSQL exige `SET col = TRUE|FALSE`: rechaza `SET col IS TRUE` (sintaxis) y `SET col = 1` (`boolean = integer`). El defecto solo se manifiesta contra un servidor real, por eso los tests del adapter con `MagicMock` no lo detectaban.

La corrección separa los dos contextos: predicados (`col = 1` → `col IS TRUE`, `col = 0` → `NOT col`) y asignaciones (`col = 1` → `col = TRUE`, `col = 0` → `col = FALSE`). También unifica la lista de columnas booleanas en `_BOOLEAN_COLUMNS` (había 3 copias) y hace que el predicado `NOT` respete el calificador (`a.active = 0` → `NOT a.active`, antes `a.NOT active`).

Efecto medido sobre la suite completa, comparando el mismo run con y sin el cambio: **126 → 100 fallos, 26 corregidos y 0 introducidos**. Los 4 archivos con cambio son exactamente los que contienen literales booleanos en `SET` (`test_notifications_hardening` −10, `test_public_api_service_isolation` −9, `test_emails_etapa4` −6, `test_multi_tenant_admin` −1, este último por el logout, que llama `revoke_all_sessions_scoped`). Los 100 fallos restantes corresponden a archivos de A/C/E aún no migrados y a limitaciones de Windows (symlinks, `PermissionError` al borrar temporales SQLite, tipos `date`/`time` de PostgreSQL frente a strings de SQLite); **son independientes de esta corrección**.

## Consumidores SQLite comprobados (no eliminados)

- `database/database.py`: conserva `DATABASE_PATH` y `sqlite3.connect` para el backend SQLite explícito.
- `database/migrator.py`: abre un archivo SQLite como origen ETL SQLite → PostgreSQL.
- `scripts/backup_database.py`, `verify_backup.py`, `restore_database.py`, `prune_backups.py`: consumen `DATABASE_PATH`, el formato `.db`, PRAGMA/WAL o nombres de backup SQLite.
- `deploy/systemd/turnobot-backup-prune.timer` activa `turnobot-backup-prune.service`, que ejecuta `scripts/prune_backups.py --apply`; es un consumidor operativo confirmado en el repositorio.
- Los timers/services de backup diario y verificación semanal ya ejecutan `backup_postgresql.py` y `verify_backup_postgresql.py` respectivamente. No se inspeccionó ni modificó el host remoto.
- `.github/workflows/tests.yml` configura `TURNOBOT_PG_URL` para el paso `tests_pg`, pero no en el paso posterior `pytest tests`. Revisar ese contrato de CI en la fase de harness; no se cambió el workflow aquí.
- README y DEPLOYMENT aún documentan herramientas SQLite legacy además de backup PostgreSQL. No se eliminaron referencias porque los scripts/timer tienen consumidores vigentes.

## Orden de migración recomendado

Ya completados (16 archivos): `test_business_settings`, `test_security_concurrency`, `test_ai_model`, `test_notifications_conditional`, `test_onboarding`, `test_knowledge`, `test_notifications`, `test_services_isolation`, `test_security_operations`, `test_membership_management`, `test_membership_http`, `test_staff_invitation`, `test_membership_hardening`, `test_membership_ui`, `test_resources_appointments`, `test_password_reset`. `test_connection_usage` queda excluido por ser deuda de rediseño (E).

Pendiente, primero archivos pequeños/medianos con intención funcional clara y estado limitado; reutilizar `PostgreSQLTestCase`/fixtures existentes:

1. `test_standalone_create_app.py`, `test_rate_limit_http.py`, `test_rate_limiting.py`, `test_resources_isolation.py`, `test_registro_publico.py`, `test_ai_tools_integration.py`, `test_emails_etapa4.py`.
2. `test_conversations.py`, `test_multi_tenant_admin.py`, `test_notifications_hardening.py`, `test_platform.py`, `test_provision_business.py`, `test_membership_authorization.py`, `test_resources_public_api.py`.
3. `test_observability.py` y `test_public_api_service_isolation.py` después de dividir por módulos/fixtures; son los dos archivos más grandes y mezclan varios niveles de infraestructura.

`test_fase_4f_query_adaptation.py`, `test_fase_4g_transaction_isolation.py` y `test_pg_pool_hardening.py` requieren separar primero los casos unitarios del adapter de los escenarios funcionales.

## Límites de esta revisión

- Las cifras provienen de una **colección real** (`pytest tests/ tests_pg/ --collect-only -q`) ejecutada con `TURNOBOT_PG_URL` apuntando a la base de tests local. No se usó una URL ficticia ni una base de producción.
- La colección confirma **81 archivos / 1.089 items**; los totales por categoría reconcilian exactamente contra ese número, y cada archivo está clasificado una sola vez (verificado sin duplicados, sin sobrantes y sin archivos sin clasificar).
- Conteo independiente por **AST** (funciones y métodos `test_*`): **81 archivos / 1.088 tests**. La única diferencia frente a pytest es `tests/test_cli_pg_pool_lifecycle.py` (AST 9, colección 10) por el caso parametrizado ya documentado; `1.088 + 1 = 1.089`. No hay ninguna otra divergencia.
- El incremento de 1.077 → 1.089 se explica íntegramente por los 12 tests nuevos del adapter; las migraciones posteriores movieron archivos de categoría sin alterar el número total de tests. Ningún test cambió de categoría por otra razón que no sea su migración efectiva a PostgreSQL.
- No se cambió ninguna prueba, fixture, servicio, configuración, sistema remoto ni archivo de producción al actualizar este inventario.

## Drift detectado fuera de esta tarea (requiere decisión)

`services/loyalty.py` aparece modificado en el working tree y **no** lo fue por esta actualización del inventario. El cambio está en `retention_candidates()` y es una corrección legítima de tipo PostgreSQL, no un workaround:

- `migrations_pg/001_initial_schema.sql:387` declara `last_completed_date DATE`, por lo que psycopg devuelve `datetime.date`; en SQLite el mismo valor era `str`.
- El código original hacía `date.fromisoformat(item["last_completed_date"])`, que con un `date` lanza **`TypeError`**, y el `except ValueError` del bloque no lo captura: el fallo escapaba como `TypeError` en vez de degradarse.
- El código actual acepta ambos tipos, preservando los dos backends. Lo ejercita `tests/test_loyalty_10_2_10_3.py` (8 tests, categoría B, verde) y es alcanzable desde `routes/admin.py:973`.

Se deja registrado y **sin revertir**: revertirlo devolvería `test_loyalty_10_2_10_3.py` a un crash por `TypeError` en PostgreSQL. Requiere confirmar si su introducción quedó dentro del alcance autorizado.
