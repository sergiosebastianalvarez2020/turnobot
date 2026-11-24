# Plan TurnoBot V1

> Hoja de ruta oficial de TurnoBot V1. Este documento es la referencia para futuras
> sesiones de desarrollo y para cualquier agente de IA que trabaje en el repositorio.
>
> **├Ültima actualizaci├│n:** 2026-11-06 (revisi├│n 2, ajustes del informe de revisi├│n)
>
> **Ubicaci├│n actual:** `C:\Proyectos\turnobot\docs\product\PLAN_TURNOBOT_V1.md`.
> Todav├¡a **no** est├í en `C:\Proyectos\turnobot-main-clean`; su traslado e
> integraci├│n quedan pendientes y no se ejecutaron en esta revisi├│n.
>
> **Estado general:** V1 **no est├í lista para comercializar**. Hay funcionalidades
> existentes y otras parcialmente implementadas; el lanzamiento depende de cerrar
> los pendientes de validaci├│n, preproducci├│n y proceso comercial. La correcci├│n de
> rutas de dominios es higiene t├®cnica no bloqueante.
>
> **Naturaleza del documento:** planificaci├│n y estado. No modifica c├│digo funcional,
> no agrega dependencias y no autoriza despliegues, migraciones ni cambios en producci├│n.

## Convenciones de estado

| Etiqueta | Significado |
|---|---|
| `[EXISTENTE]` | Implementado y con evidencia en c├│digo, pruebas o documentaci├│n verificable. No implica que est├® validado en todos los entornos. |
| `[PARCIAL]` | Hay implementaci├│n, pero falta una parte funcional, su exposici├│n, una prueba o una validaci├│n cr├¡tica. |
| `[PENDIENTE]` | Requisito de V1 que todav├¡a no est├í implementado o no tiene proceso operativo definido. |
| `[EN CURSO]` | Trabajo iniciado, con avance local o parcial, pero sin cumplir todav├¡a su criterio de salida. |
| `[FUTURO]` | Fuera del alcance de V1; se documenta para no confundirlo con deuda de lanzamiento. |

## Base de evidencia y l├¡mites de esta revisi├│n

Esta hoja de ruta se apoya en la documentaci├│n y el c├│digo del repositorio, no en
suposiciones de producto:

- `README.md`, `AGENTS.md`, `DEPLOYMENT.md`, `CUTOVER_PROCEDURE.md`.
- `docs/custom-domains.md`, `docs/admin_dashboard_audit.md`,
  `docs/TEST_SUITE_INVENTORY.md`.
- `routes/`, `services/`, `database/`, `migrations/`, `migrations_pg/`,
  `templates/` y `tests/`.
- `git log`, `git status`, `git stash list` y ramas locales.

L├¡mites expl├¡citos:

- **No se ejecut├│ la suite completa en esta revisi├│n.** El baseline can├│nico
  documentado en `AGENTS.md` es **1.284 passed / 0 failed / 0 skipped** sobre el
  commit `c3fee3a` (2026-10-09). `docs/TEST_SUITE_INVENTORY.md` registra una
  colecci├│n posterior de **95 archivos / 1.355 ├¡tems** y un run en verde sobre
  `7e39b24` (2026-10-12). Ninguno de esos commits es el HEAD local auditado
  (`e57ad6b`, incluido en `origin/main` `4bb5a1a`), por lo que **no se extrapola
  ese resultado al estado actual**.
- **La validaci├│n completa con Python 3.12 y PostgreSQL est├í pendiente.** El
  entorno virtual disponible en esta sesi├│n es **Python 3.13.14**, mientras que
  CI y `pyproject.toml` fijan **Python 3.12** y PostgreSQL 16. La variable
  `TURNOBOT_PG_URL` est├í definida en el entorno local, pero la suite no se ejecut├│
  en esta revisi├│n.
- **Existe un respaldo de cambios locales mezclados.** Hay tres entradas de
  `git stash` (`WIP: preserve local changes before rebase`,
  `WIP: preserve TurnoBot changes before test-8-workers` y `fix-invitacion`) y
  archivos sin seguimiento (helpers `.ps1`/`.bat`, `.env.bak`, `tests/visual/`,
  scripts sueltos, `.cleanup_backup/`, entre otros). Mezclan cambios de distintas
  ramas y ├®pocas, incluidos archivos potencialmente sensibles y experimentos.
  **No deben aplicarse en bloque, commitearse ni usarse como base de V1.**
- **Correcci├│n local de rutas de dominios (no integrada).** En
  `C:\Proyectos\turnobot-main-clean` existe una correcci├│n local sin commit en tres
  archivos: `routes/__init__.py` (importa e invoca `business_domains.register(app)`),
  `tests/test_standalone_create_app.py` (actualiza el mapa a 136 rules / 103
  endpoints y valida las reglas de dominios) y
  `tests/test_business_domain_route_registration.py` (nuevo; valida el registro de
  las 18 rutas y el rechazo 401 sin autorizaci├│n). Seg├║n la ├║ltima ejecuci├│n local,
  pasaron **20 tests** con **Python 3.13.14**;
  `python -m ruff format --check --exclude ".venv*" .` inform├│ **208 files already
  formatted** y `python -m ruff check --exclude ".venv*" .` inform├│ **All checks
  passed!**. Esa evidencia de lint/formato **no sustituye** la suite completa ni CI.
  **Faltan:** validaci├│n con PostgreSQL, ejecuci├│n en Python 3.12, CI, commit e
  integraci├│n en GitHub, y la matriz completa de autorizaci├│n/aislamiento/CSRF (el
  401 aislado no alcanza). El commit `4bb5a1a` no registraba estas rutas.
- **La marca no est├í unificada.** La landing comercial usa **TurnoBot**
  (`templates/marketing_home.html`), mientras que `README.md` y varios documentos
  heredados usan **TurnoGo**. Es una tarea de documentaci├│n y comunicaci├│n, no un
  cambio funcional de V1.

---

## 1. Visi├│n y objetivo comercial de TurnoBot V1

### Visi├│n

TurnoBot es una plataforma SaaS multi-tenant para negocios que trabajan con turnos,
atenciones o servicios. Su propuesta es centralizar la operaci├│n diaria en un solo
lugar: p├ígina p├║blica de reservas, agenda, gesti├│n de servicios y recursos, atenci├│n
por chat con IA, fidelizaci├│n y m├®tricas operativas.

### Objetivo comercial de V1

Poner a disposici├│n de negocios reales una versi├│n **estable, aislada por negocio y
operable con procesos manuales controlados**, que permita:

1. Dar de alta un negocio con aprobaci├│n manual.
2. Publicar su p├ígina de reservas en el dominio compartido de TurnoBot.
3. Recibir y gestionar reservas, cancelaciones y reprogramaciones.
4. Usar el panel administrativo para operar el negocio.
5. Permitir que el Superadmin active o suspenda el servicio.
6. Cobrar una suscripci├│n mensual por un proceso manual, sin pasarela de pagos.

### Perfil inicial

Negocios peque├▒os y medianos con agenda de turnos: barber├¡as, centros de est├®tica,
consultorios, clases, talleres y servicios profesionales. La landing comercial ya
apunta a esos rubros y el alta se solicita por WhatsApp o por el formulario p├║blico
`/registro`.

### M├®tricas de ├®xito propuestas para V1

Son objetivos de validaci├│n comercial, no afirmaciones de resultado:

- Tiempo de alta asistida menor a un d├¡a h├íbil una vez aprobado el negocio.
- Un negocio piloto puede operar reservas y gesti├│n diaria sin intervenci├│n t├®cnica.
- Cero incidentes cr├¡ticos de aislamiento entre negocios durante el piloto.
- Cobro manual y suspensi├│n/activaci├│n ejecutables sin tocar la base de datos.
- El negocio puede volver a operar tras una suspensi├│n sin p├®rdida de datos.

---

## 2. Funcionalidades para los negocios

### 2.1 P├ígina p├║blica y reservas

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Landing p├║blica por negocio en `/b/<slug>` | `[EXISTENTE]` | `routes/public.py`, `templates/landing.html`; el negocio se resuelve por slug. |
| P├ígina comercial de la plataforma en `/` | `[EXISTENTE]` | `templates/marketing_home.html`; incluye CTA por WhatsApp. |
| Wizard de reserva en 4 pasos | `[EXISTENTE]` | `routes/public.py`: servicio, fecha, datos y confirmaci├│n. |
| Reserva, cancelaci├│n y reprogramaci├│n p├║blica | `[EXISTENTE]` | `/api/reservar`, `/api/cancelar`, `/api/reprogramar`; gesti├│n con token. |
| L├¡mite anti-abuso por contacto | `[EXISTENTE]` | Se aplica dentro de la transacci├│n de reserva; cubierto por la suite documentada. |
| Protecci├│n honeypot, timestamp e idempotencia en el wizard | `[EXISTENTE]` | `routes/public.py` y pruebas asociadas. |
| Disponibilidad por fecha y recursos | `[EXISTENTE]` | `/api/disponibilidad/<fecha>`, `/api/recursos`. |
| P├ígina para gestionar un turno con token | `[EXISTENTE]` | `/b/<slug>/turno/<token>?id=<id>`; `templates/gestionar_turno.html`. |

### 2.2 Recepcionista con IA

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Chat con Google Gemini | `[EXISTENTE]` | `services/ai.py`, `/chat`; requiere `GEMINI_API_KEY`. |
| Herramientas de disponibilidad, recursos, reserva, b├║squeda, cancelaci├│n, reprogramaci├│n y escalado humano | `[EXISTENTE]` | Siete herramientas documentadas en `README.md`. |
| Confirmaci├│n de acciones mutantes y l├¡mites de iteraciones/historial | `[EXISTENTE]` | Guardas en el servicio de IA y pruebas documentadas. |
| Handoff a atenci├│n humana y moderaci├│n de conversaciones | `[EXISTENTE]` | Panel de conversaciones y estados. |
| Dependencia de un proveedor externo de IA | `[PARCIAL]` | Si falta la clave o el proveedor falla, la disponibilidad del chat no est├í garantizada; debe existir un modo degradado expl├¡cito para V1. |

### 2.3 Panel administrativo del negocio

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Login, logout, invitaciones y recuperaci├│n de contrase├▒a | `[EXISTENTE]` | `routes/auth.py`, `routes/auth_public.py`, `routes/invitation.py`. |
| Roles owner/admin/staff y gesti├│n de usuarios | `[EXISTENTE]` | `services/memberships.py`, `routes/admin.py`. |
| Agenda y gesti├│n de turnos | `[EXISTENTE]` | Cancelar, cambiar estado, reprogramar, crear turno manual. |
| Servicios y horarios semanales | `[EXISTENTE]` | CRUD de servicios y guardado de horarios. |
| Recursos reservables | `[EXISTENTE]` | Crear, activar/desactivar y asignar a turnos. |
| Fidelizaci├│n y recompensas | `[EXISTENTE]` | Puntos, ledger, configuraci├│n, cat├ílogo y canjes. |
| Base de conocimiento | `[EXISTENTE]` | CRUD y b├║squeda full-text; PostgreSQL `tsvector`/GIN y fallback SQLite FTS5. |
| Conversaciones y escalado humano | `[EXISTENTE]` | Listado, detalle, resoluci├│n y cambio de estado. |
| M├®tricas operativas | `[EXISTENTE]` | `/admin/metricas`: per├¡odo anterior, servicios top, demanda por d├¡a/hora, clientes, chat y fidelizaci├│n. |
| Anal├¡tica de IA y oportunidades | `[EXISTENTE]` | `/admin/inteligencia`, `/admin/inteligencia/oportunidades`. |
| Redise├▒o integral del panel | `[PARCIAL]` | `docs/admin_dashboard_audit.md` marca solo la auditor├¡a como cerrada; el plan por fases (dashboard, agenda, clientes, servicios, empleados, estad├¡sticas, notificaciones, integraci├│n) no est├í cerrado. La rama `feature/admin-panel-redesign` ya incorpora nueva UI, sidebar y tokens. |
| M├®tricas de ingresos, facturaci├│n u ocupaci├│n estimada | `[FUTURO]` | El propio panel declara que no hay ingresos ni ocupaci├│n estimada porque no existe precio hist├│rico fiable. |

### 2.4 Notificaciones y operaci├│n

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Confirmaciones y recordatorios por email | `[EXISTENTE]` | `services/notifications.py`, `scripts/send_reminders.py`; requiere SMTP configurado. |
| Reintentos de notificaciones | `[EXISTENTE]` | Pol├¡tica de reintentos y tablas asociadas. |
| Health check con conectividad a base | `[EXISTENTE]` | `/health`, `scripts/check_health.py`. |
| Backups PostgreSQL y verificaci├│n del dump | `[EXISTENTE]` | `scripts/backup_postgresql.py`, `scripts/verify_backup_postgresql.py`. |
| Restauraci├│n segura en base aislada | `[EXISTENTE]` | `scripts/restore_postgresql.py`; rechaza el destino de producci├│n. |
| Panel de dominio personalizado | `[PARCIAL]` | Ver secci├│n 5: hay modelo, resolver, plantilla y pruebas de datos. La correcci├│n de rutas es higiene t├®cnica local en `turnobot-main-clean`. La activaci├│n comercial de dominios propios es `[FUTURO]` y queda **fuera de V1**. |

---

## 3. Funcionalidades del panel Superadmin

El Superadmin es una identidad de plataforma separada de los usuarios de negocio.
Vive en `platform_users` y opera con prefijos `platform_*`; no es tenant-scoped por
dise├▒o (`services/platform.py`).

### 3.1 Funcionalidades existentes

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Login y logout separados de la autenticaci├│n de negocio | `[EXISTENTE]` | `/superadmin/login`, `/superadmin/logout`; sesi├│n de plataforma y token persistente. |
| Protecci├│n CSRF y rate limit de login | `[EXISTENTE]` | `valid_csrf_token`, `is_login_request_allowed`. |
| Listado de negocios con estado | `[EXISTENTE]` | `list_businesses_for_platform`, `templates/superadmin.html`. |
| Crear solicitud de alta de negocio | `[EXISTENTE]` | Crea negocio pendiente y owner pendiente; no genera invitaci├│n todav├¡a. |
| Aprobar solicitud y generar invitaci├│n del owner | `[EXISTENTE]` | `approve_business`; el negocio queda activo y el owner define contrase├▒a por invitaci├│n. |
| Reenviar invitaci├│n | `[EXISTENTE]` | Revoca invitaciones activas y genera una nueva. |
| Suspender y reactivar negocio | `[EXISTENTE]` | `set_business_active`; el resolver p├║blico exige `active` y los dominios personalizados exigen negocio activo. |
| Ver detalle del negocio, configuraci├│n, miembros e invitaciones | `[EXISTENTE]` | `superadmin_negocios_detalle`. |
| Auditor├¡a de acciones de plataforma | `[EXISTENTE]` | `audit_log`, `/superadmin/auditoria`, `log_platform_action`. |
| Bootstrap de Superadmin por CLI | `[EXISTENTE]` | `scripts/create_superadmin.py`. |

### 3.2 Funcionalidades parciales o pendientes

| Funcionalidad | Estado | Notas |
|---|---|---|
| Estado comercial del negocio (plan, precio, vencimiento, estado de cobro) | `[PENDIENTE]` | No hay modelo de suscripci├│n ni campos de facturaci├│n en el esquema ni en el panel. |
| Historial de pagos y comprobantes | `[PENDIENTE]` | El cobro es manual y fuera de la aplicaci├│n; no hay registro dentro del panel. |
| Suspensi├│n autom├ítica por falta de pago | `[FUTURO]` | V1 usa suspensi├│n manual desde Superadmin. |
| Motivo estructurado de suspensi├│n | `[PENDIENTE]` | Hoy la acci├│n se registra en auditor├¡a, pero no hay un campo de motivo comercial normalizado. |
| M├®tricas de uso por negocio para facturaci├│n o salud comercial | `[FUTURO]` | No es requisito de V1; el panel de negocio ya tiene m├®tricas operativas. |
| Suplantaci├│n o impersonaci├│n controlada | `[FUTURO]` | No est├í implementada; si se evaluara, requerir├¡a auditor├¡a y l├¡mites estrictos. |
| Gesti├│n de dominios personalizados desde Superadmin | `[FUTURO]` | La gesti├│n actual de dominios es del owner; no hay consola de plataforma para aprovisionar ingress/TLS. |

---

## 4. Arquitectura multi-tenant y aislamiento de datos

### 4.1 Modelo actual

`[EXISTENTE]` La aplicaci├│n es multi-tenant por `business_id`:

- El tenant se resuelve desde el slug de la URL (`/b/<slug>`) o desde un dominio
  personalizado verificado por Host.
- `g.current_business` y `get_current_business_id()` son la fuente del negocio
  actual; las rutas administrativas no aceptan `business_id` desde formularios
  para decidir el tenant.
- Las consultas de dominio usan funciones `*_scoped` que reciben `business_id` y
  filtran por ├®l (`database/database.py`).
- La autenticaci├│n de negocio usa membres├¡as (`business_users`); owner/admin/staff
  se validan contra el negocio resuelto.
- La autenticaci├│n de plataforma es independiente (`platform_users`) y no se mezcla
  con membres├¡as de negocio.
- El esquema PostgreSQL declara `business_id` con claves for├íneas y restricciones
  por tabla (`migrations_pg/001_initial_schema.sql` y `003_business_domains.sql`).

### 4.2 Controles de aislamiento

`[EXISTENTE]`

- `_require_admin_membership()` y `membership_required(...)` verifican sesi├│n y rol
  antes de operar (`routes/admin.py`).
- La gesti├│n de dominios exige rol `owner` y obtiene el negocio desde el contexto,
  nunca desde el formulario (`routes/business_domains.py`).
- Los dominios personalizados solo resuelven si est├ín verificados, activos, con TLS
  validado y asociados a un negocio activo (`database/business_domains.py`).
- Un host desconocido no cae al negocio `id=1`; el Host de plataforma solo se usa
  para `localhost`/`127.0.0.1` y el resto responde 404 (`application/tenant.py`).
- `PUBLIC_BASE_URL` es el origen de confianza para enlaces absolutos; no se
  construyen enlaces desde el Host entrante (`application/public_urls.py`).
- Las cookies de sesi├│n son host-only: un login en un hostname no se comparte con
  otro.

### 4.3 Riesgos y validaciones pendientes

- `[PARCIAL]` La suite de aislamiento y multi-tenant est├í documentada, pero el HEAD
  actual no tiene una ejecuci├│n completa registrada en Python 3.12 + PostgreSQL.
- `[EN CURSO]` Las rutas administrativas de dominios ya tienen una correcci├│n local
  sin commit en `turnobot-main-clean` (tres archivos, 20 tests reportados en Python
  3.13.14). Falta validarlas con PostgreSQL, Python 3.12 y CI, y cubrir de forma
  expl├¡cita autorizaci├│n, aislamiento y CSRF en el entorno soportado. Esta
  correcci├│n es **higiene t├®cnica**: no convierte la activaci├│n de dominios propios
  en requisito comercial de V1.
- `[PENDIENTE]` Validar el comportamiento de suspensi├│n sobre sesiones ya abiertas,
  enlaces p├║blicos y dominios personalizados en un entorno real.
- `[FUTURO]` Row-Level Security (RLS) de PostgreSQL, aislamiento por esquema o
  particionamiento no forman parte de V1; el aislamiento actual es de aplicaci├│n.
- `[PARCIAL]` El rate limiting es en memoria y por proceso. Si se escala
  horizontalmente, cada instancia tiene su propio estado; V1 no depende de escalado
  horizontal, pero la limitaci├│n debe quedar documentada en operaciones.

---

## 5. Modalidades de publicaci├│n

### 5.1 Dominio compartido como opci├│n inicial

`[EXISTENTE]` Es la modalidad de lanzamiento de V1.

- Cada negocio se publica en `PUBLIC_BASE_URL/b/<slug>`.
- La landing comercial de la plataforma queda en `/`.
- El negocio por defecto (`id=1`) puede resolverse en `/` cuando corresponde.
- No requiere DNS propio, certificado propio ni cambios de ingress por cliente.
- Los enlaces de reserva, invitaci├│n y gesti├│n usan el origen p├║blico configurado.
- Limitaci├│n aceptada: la URL contiene el slug de TurnoBot y no la marca del cliente.

**Decisi├│n de V1:** el dominio compartido es suficiente para comercializar. Los
dominios personalizados **no son un requisito de lanzamiento ni se ofrecen en el
plan base**. La correcci├│n de las rutas administrativas es solo higiene t├®cnica para
evitar un enlace roto; no habilita la activaci├│n comercial de dominios propios.

### 5.2 Dominios personalizados como funcionalidad futura

**Estado:** la funci├│n comercial es `[FUTURO]`; la correcci├│n de las rutas
administrativas es `[PARCIAL]` y se trata como higiene t├®cnica no bloqueante.

Hay una implementaci├│n adelantada, pero la activaci├│n comercial **no est├í lista
para lanzamiento**:

- `[EXISTENTE]` Tabla `business_domains` y migraciones:
  - SQLite: `migrations/025_business_domains.sql`.
  - PostgreSQL: `migrations_pg/003_business_domains.sql`.
- `[EXISTENTE]` Resoluci├│n de tenant por Host en `application/tenant.py`.
- `[EXISTENTE]` Verificaci├│n de propiedad por registro TXT y comprobaci├│n TLS en
  `application/business_domains.py`.
- `[EXISTENTE]` Panel de owner en `templates/business_domains.html`, con acciones
  para agregar, verificar, rotar token, comprobar TLS, activar, desactivar, revocar
  y elegir dominio principal.
- `[EXISTENTE]` Pruebas de repositorio y de resoluci├│n en
  `tests/test_business_domains.py`, `tests/test_business_domain_routes.py` y
  `tests_pg/test_business_domains_live.py`.
- `[EN CURSO]` **Correcci├│n de rutas administrativas de dominios.**
  `routes/business_domains.py` define las vistas y una funci├│n `register(app)`.
  El commit `4bb5a1a` **no** importaba ni invocaba ese registro, por lo que la
  pantalla enlazada desde `templates/admin.html` no quedaba expuesta. En
  `turnobot-main-clean` hay una **correcci├│n local sin commit** en tres archivos:
  `routes/__init__.py` (registra `business_domains.register(app)`),
  `tests/test_standalone_create_app.py` (actualiza el mapa de rutas y valida las
  reglas de dominios) y `tests/test_business_domain_route_registration.py` (nuevo;
  valida las 18 rutas y el rechazo 401). Seg├║n la ├║ltima ejecuci├│n local en
  `turnobot-main-clean`, pasaron **20 tests** con **Python 3.13.14**;
  `python -m ruff format --check --exclude ".venv*" .` inform├│ **208 files already
  formatted** y `python -m ruff check --exclude ".venv*" .` inform├│ **All checks
  passed!**. Esa evidencia de lint/formato **no sustituye** la suite completa ni CI.
  **Faltan:** validaci├│n PostgreSQL, Python 3.12, CI, commit e integraci├│n.
- `[PENDIENTE]` **Matriz completa de seguridad de rutas.** La prueba local de 401
  solo verifica el rechazo sin sesi├│n; **no** demuestra autorizaci├│n por rol
  (owner/admin/staff), aislamiento entre negocios, CSRF, rutas con y sin slug ni
  errores de registro. Esa matriz debe ejecutarse en el entorno soportado antes de
  declarar la validaci├│n terminada.
- `[FUTURO]` Emisi├│n y renovaci├│n autom├ítica de certificados, aprovisionamiento de
  Nginx/ingress, allowlist de hostnames y automatizaci├│n de DNS.
- `[FUTURO]` Dominios comod├¡n, resoluci├│n por sufijo o m├║ltiples niveles no est├ín
  soportados.

**Decisi├│n de V1:** los dominios personalizados quedan **fuera del alcance inicial
de lanzamiento**. No se ofrecen como parte del plan base ni se activan en clientes
reales hasta que exista un proceso operativo de certificados e ingress validado.
La correcci├│n de rutas administrativas es **higiene t├®cnica no bloqueante**: se hace
para que el panel no tenga un enlace roto, no para habilitar la funci├│n comercial en
V1.

### 5.3 Nginx y Let's Encrypt como alternativa t├®cnica prevista

`[FUTURO]` Para una etapa posterior a V1, la documentaci├│n de dominios contempla dos
alternativas de ingress:

1. **AWS ALB + ACM** (evaluar costo, validaci├│n DNS y cambios de entrada).
2. **Nginx en EC2 + ACME operado por el equipo**, con Let's Encrypt como opci├│n
   prevista.

La alternativa Nginx + Let's Encrypt requerir├¡a:

- Emisi├│n, renovaci├│n, recarga y revocaci├│n de certificados.
- L├¡mites de emisi├│n y manejo de fallos de ACME.
- Allowlist de hostnames provisionados.
- Terminaci├│n TLS por SNI y conservaci├│n controlada del Host autorizado.
- Reemplazo de cabeceras reenviadas del cliente y `TRUSTED_PROXY_COUNT` correcto.
- Proceso de verificaci├│n TLS desde el panel despu├®s de instalar el certificado.

`docs/custom-domains.md` aclara que la aplicaci├│n **todav├¡a no automatiza** la
emisi├│n/renovaci├│n de certificados ni provisiona Nginx, y que los dominios reales no
deben activarse hasta que exista y se valide ese proceso operativo.

---

## 6. Modelo comercial inicial

### 6.1 Alta manual

`[EXISTENTE]` el flujo t├®cnico; `[PARCIAL]` el modelo comercial asistido.

Flujo actual:

1. El interesado contacta por WhatsApp desde la landing o completa `/registro`.
2. Se crea un negocio en estado **pendiente** (`active=0`, `pending=1`) con un owner
   pendiente.
3. El Superadmin revisa la solicitud y la **aprueba manualmente**.
4. La aprobaci├│n activa el negocio y genera una invitaci├│n ├║nica para que el owner
   defina su contrase├▒a.
5. El owner acepta la invitaci├│n y accede al panel.

Notas:

- El formulario p├║blico no activa el negocio por s├¡ solo.
- El Superadmin tambi├®n puede crear una solicitud desde `/superadmin/negocios/crear`.
- La aprobaci├│n, la activaci├│n y la suspensi├│n son **exclusivas del Superadmin**;
  ning├║n owner/admin/staff puede activar su propio negocio ni cambiar su estado
  comercial.
- El alta y la aprobaci├│n siguen siendo **manuales**; no hay onboarding autom├ítico
  ni pago previo.

### 6.2 Suscripci├│n mensual

`[PENDIENTE]` Es el modelo comercial definido para V1, pero **no est├í implementado**.

El modelo es una **suscripci├│n mensual con cobro manual**: se factura y cobra por un
canal externo, sin recurrencia autom├ítica y sin pasarela de pagos integrada. La
"recurrencia" existe solo a nivel comercial y operativo manual; no hay d├®bitos
autom├íticos ni cobros programados por el sistema.

- No hay tabla de planes, suscripciones, precios, moneda, ciclo de facturaci├│n ni
  vencimientos.
- No hay estados comerciales (`trial`, `activo`, `moroso`, `cancelado`) en el
  esquema.
- No hay prorrateo, cambios de plan ni autogesti├│n de la suscripci├│n.
- La definici├│n de precio, moneda, impuestos y condiciones queda fuera de este
  documento y debe aprobarse antes del lanzamiento.

### 6.3 Cobro manual inicialmente

`[PENDIENTE]` V1 no integra pasarela de pagos ni recurrencia autom├ítica.

- La suscripci├│n es mensual, pero el cobro se ejecuta y registra manualmente; no hay
  d├®bito autom├ítico ni cobros programados.
- El cobro lo ejecuta el responsable comercial designado por TurnoBot por un canal
  externo (transferencia, efectivo u otro medio acordado).
- No hay registro autom├ítico del pago dentro de TurnoBot.
- No hay conciliaci├│n autom├ítica dentro de TurnoBot, ni generaci├│n de comprobantes,
  ni bloqueo autom├ítico por falta de pago.
- La emisi├│n de comprobantes fiscales y el cumplimiento impositivo quedan fuera de
  TurnoBot; el negocio los gestiona con su contador o sistema externo.
- Cualquier integraci├│n de pagos (Mercado Pago, Stripe u otra) es `[FUTURO]` y no
  debe agregarse como dependencia de V1 sin una decisi├│n expl├¡cita.

**Registro inicial de pagos:** puede llevarse **fuera de TurnoBot** en una planilla
controlada (por ejemplo, una hoja de c├ílculo con acceso restringido y respaldo).
Datos m├¡nimos por fila:

- negocio y `business_id` o slug;
- per├¡odo cubierto (mes/a├▒o) y fecha de vencimiento;
- importe y moneda;
- fecha de pago y medio/canal (transferencia, efectivo u otro);
- referencia o comprobante (n├║mero de operaci├│n, enlace o archivo adjunto);
- estado (`pendiente`, `pagado`, `vencido`, `excepcional`);
- qui├®n registr├│ y qui├®n verific├│;
- observaciones.

**Verificaci├│n:** un responsable designado (Superadmin o responsable comercial
autorizado) contrasta cada pago con el extracto bancario o comprobante y marca la
fila como verificada. Cuando sea posible, quien cobra no debe ser la ├║nica persona
que verifica.

**Vencimientos:** se revisan al menos una vez por mes, antes de la fecha de
vencimiento. Si no hay pago, se marca `vencido`, se avisa al cliente y se aplica el
runbook comercial. La suspensi├│n es una decisi├│n manual del Superadmin; no es
autom├ítica.

### 6.4 Control de activaci├│n y suspensi├│n desde Superadmin

`[EXISTENTE]` Es el mecanismo comercial-operativo de V1 y es **exclusivo del
Superadmin**.

- El Superadmin puede **aprobar**, **suspender** y **reactivar** un negocio.
- Ning├║n usuario de negocio (owner/admin/staff) puede activar, suspender ni cambiar
  el estado comercial por ninguna v├¡a.
- La suspensi├│n pone `active=0`; el resolver p├║blico y el resolver de dominios
  exigen negocio activo.
- Las acciones quedan registradas en la auditor├¡a de plataforma.
- El owner puede ser reinvitado si todav├¡a no acept├│ la invitaci├│n.

Pendiente para que el control sea comercialmente completo:

- `[PENDIENTE]` Vincular operativamente el estado del negocio con el estado de la
  suscripci├│n y del pago manual, aunque el registro sea externo.
- `[PENDIENTE]` Definir un motivo de suspensi├│n y un procedimiento de aviso al
  cliente.
- `[PENDIENTE]` Validar en un entorno real que la suspensi├│n corta el acceso p├║blico
  y administrativo de forma inmediata y sin efectos laterales.

---

## 7. Etapas de desarrollo y validaci├│n

Las etapas son secuenciales para el lanzamiento, salvo E1 (recomendable y no
bloqueante con dominio compartido) y las que se indiquen como paralelas o posteriores
a V1. El estado refleja la auditor├¡a del 2026-11-06 y la revisi├│n 2.

| Etapa | Objetivo | Entregables | Criterio de salida | Estado |
|---|---|---|---|---|
| E0. L├¡nea base | Conocer el estado real del repositorio y separar cambios v├ílidos de experimentos. | Auditor├¡a de c├│digo, documentaci├│n, git status/stash, baseline de pruebas. | Riesgos y pendientes identificados; cambios mezclados aislados. | `[EN CURSO]` |
| E1. Higiene de rutas de dominios (no bloqueante) | Integrar la correcci├│n local de las rutas `/admin/dominios` para eliminar el enlace roto, sin habilitar la activaci├│n comercial de dominios en V1. | Commit de los tres archivos, pruebas de autorizaci├│n/aislamiento, CI y documentaci├│n actualizada. | Rutas expuestas solo para owner; pruebas en verde; CI aprobado; sin regresiones. No bloquea el lanzamiento con dominio compartido. | `[EN CURSO]` |
| E2. Validaci├│n integral | Ejecutar la suite completa en el entorno soportado. | Python 3.12, PostgreSQL 16, `TURNOBOT_PG_URL`, lint, formato, typecheck, suite general y PG. | 0 fallos nuevos; solo fallos preexistentes documentados y justificados. | `[PENDIENTE]` |
| E2.5. Preproducci├│n AWS | Desplegar y validar en un entorno equivalente en configuraci├│n (no en datos) antes del piloto. | Staging en AWS con base de datos independiente, credenciales independientes, notificaciones aisladas, mismas migraciones/configuraci├│n, `/health`, logs y rollback. | Despliegue reproducible; smoke tests verdes; migraciones y rollback probados; sin conexi├│n a la base de producci├│n ni correos reales a clientes. | `[PENDIENTE]` |
| E3. Preparaci├│n comercial | Definir y operar el modelo de suscripci├│n mensual con cobro manual. | Precio, condiciones, proceso de cobro, registro de pagos, runbook de suspensi├│n. | Un cobro manual de prueba registrado; activaci├│n/suspensi├│n ejecutables por procedimiento. | `[PENDIENTE]` |
| E4. Piloto controlado | Validar V1 con negocios reales acotados despu├®s de preproducci├│n. | 1 a 3 negocios piloto, soporte, m├®tricas de incidentes, feedback. | Operaci├│n estable durante el per├¡odo acordado; sin incidentes cr├¡ticos de aislamiento. | `[PENDIENTE]` |
| E5. Lanzamiento V1 | Comercializar con dominio compartido y procesos manuales despu├®s de preproducci├│n y piloto. | Checklist de lanzamiento, soporte, documentaci├│n de cliente, backup/restore verificado. | Todos los criterios de la secci├│n 10 cumplidos y aprobaci├│n expl├¡cita. | `[PENDIENTE]` |
| E6. Dominios personalizados | Habilitar dominios propios con TLS operado. | Ingress, ACME/Nginx o ALB/ACM, allowlist, automatizaci├│n y soporte. | Proceso de certificados y rollback validado en producci├│n. | `[FUTURO]` |

**Dependencias y alcance:** E2.5 es obligatoria antes de E4 (piloto) y E5
(lanzamiento); no se despliega en AWS producci├│n sin E2 y E2.5 aprobadas. E2.5 usa
base de datos, credenciales y notificaciones independientes: no se conecta a la base
de producci├│n ni env├¡a correos reales a clientes. E1 es higiene t├®cnica
**recomendable y no bloqueante** para el lanzamiento con dominio compartido. E6
queda fuera de V1.

### 7.1 Matriz de validaci├│n

| Validaci├│n | Estado actual | Qu├® falta |
|---|---|---|
| Python 3.12 | `[PENDIENTE]` | Hay 20 tests locales reportados en Python 3.13.14; falta ejecutar la suite completa en 3.12 seg├║n CI. |
| PostgreSQL 16 | `[PENDIENTE]` | `TURNOBOT_PG_URL` est├í definida, pero no se ejecut├│ la suite completa en esta revisi├│n. |
| Suite general `tests/` | `[PENDIENTE]` | Ejecuci├│n completa sobre el HEAD actual. |
| Suite PostgreSQL `tests_pg/` | `[PENDIENTE]` | Ejecuci├│n completa sobre el HEAD actual. |
| Lint y formato | `[PARCIAL]` | En `turnobot-main-clean`, `python -m ruff format --check --exclude ".venv*" .` ÔåÆ **208 files already formatted**; `python -m ruff check --exclude ".venv*" .` ÔåÆ **All checks passed!**. Falta reproducirlo sobre el commit candidato; no sustituye la suite completa ni CI. |
| Typecheck | `[PENDIENTE]` | `python -m mypy app.py`. |
| Migraciones PG | `[PENDIENTE]` | `python scripts/migrate_pg.py --dry-run` y aplicaci├│n controlada en entorno aislado. |
| Preproducci├│n AWS | `[PENDIENTE]` | Staging con base de datos y credenciales independientes y notificaciones aisladas; smoke tests, secretos/SMTP/health y rollback. Sin conexi├│n a la base de producci├│n ni correos reales a clientes. |
| Smoke tests post-despliegue | `[PENDIENTE]` | Verificar en el entorno desplegado que reservas, login, admin, health y notificaciones responden. |
| Backup y restore | `[PARCIAL]` | Herramientas existentes; falta prueba de restore completa en base aislada para el estado actual. |
| DNS/TLS de dominios | `[PENDIENTE]` | Probar con un hostname real, certificado v├ílido e ingress controlado. |
| Fallos preexistentes Windows | `[EXISTENTE]` | Dos pruebas de symlink documentadas en `AGENTS.md`; no deben confundirse con regresiones nuevas. |

---

## 8. Tareas pendientes y riesgos

### 8.1 Tareas pendientes priorizadas

| ID | Prioridad | Tarea | Estado | Criterio de cierre |
|---|---|---|---|---|
| P-01 | Media (higiene, recomendable, no bloqueante de V1) | Integrar la correcci├│n local de rutas de dominios (tres archivos en `turnobot-main-clean`) y completar la matriz de autorizaci├│n/aislamiento/CSRF. | `[EN CURSO]` | Commit integrado, CI verde y PostgreSQL/Python 3.12 validados; matriz completa aprobada. El caso 401 aislado no alcanza. No bloquea el lanzamiento con dominio compartido. |
| P-02 | Alta | Ejecutar la suite completa en Python 3.12 + PostgreSQL 16 sobre el HEAD actual y obtener CI verde. | `[PENDIENTE]` | Resultado documentado, sin fallos nuevos y CI aprobado en el commit candidato. |
| P-03 | Alta | Validar migraciones 025 (SQLite) y 003 (PostgreSQL) en entorno aislado. | `[PENDIENTE]` | Dry-run, aplicaci├│n y verificaci├│n sin p├®rdida de datos. |
| P-04 | Alta | Definir precio, moneda, condiciones y proceso de cobro manual. | `[PENDIENTE]` | Documento comercial aprobado, planilla de pagos con datos m├¡nimos y runbook operativo. |
| P-05 | Alta | Probar activaci├│n, suspensi├│n y reactivaci├│n con un negocio real o aislado. | `[PENDIENTE]` | Acceso p├║blico y administrativo se corta y se restaura seg├║n lo esperado. |
| P-06 | Media | Validar DNS/TXT y TLS con un hostname real. | `[PENDIENTE]` | Verificaci├│n exitosa y fallo seguro ante certificado inv├ílido. |
| P-07 | Media | Documentar el procedimiento de backup/restore y ejecutar una prueba de restore. | `[PARCIAL]` | Restore en base aislada verificado. |
| P-08 | Media | Unificar la marca TurnoBot/TurnoGo en documentaci├│n y textos visibles. | `[PENDIENTE]` | Sin referencias contradictorias en material de cliente. |
| P-09 | Media | Revisar selectivamente stashes y archivos locales mezclados. | `[PENDIENTE]` | Nada se aplica en bloque; secretos y experimentos descartados o aislados. |
| P-10 | Baja | Cerrar las fases restantes del redise├▒o del panel admin. | `[PARCIAL]` | Plan de `docs/admin_dashboard_audit.md` completo y probado. |
| P-11 | Baja | Definir modo degradado del chat si Gemini no est├í disponible. | `[PENDIENTE]` | La reserva p├║blica sigue operativa sin IA. |
| P-12 | Alta | Desplegar en preproducci├│n AWS con base/credenciales/notificaciones independientes, ejecutar smoke tests y probar rollback. | `[PENDIENTE]` | Staging equivalente en configuraci├│n validado, sin base de producci├│n ni correos reales; rollback de aplicaci├│n y migraciones probado. |

### 8.2 Riesgos

| Riesgo | Impacto | Mitigaci├│n |
|---|---|---|
| Rutas de dominios sin integrar (correcci├│n local) | El owner ve un enlace roto; no puede gestionar dominios. | P-01 como higiene t├®cnica. No bloquea V1 con dominio compartido ni habilita la funci├│n comercial. |
| Correcci├│n local sin CI ni PostgreSQL | La correcci├│n puede fallar en el entorno soportado. | P-01: commit, CI, Python 3.12 y PostgreSQL antes de integrar. |
| Validaci├│n incompleta en Python 3.12/PostgreSQL | Un fallo espec├¡fico de producci├│n puede aparecer reci├®n al desplegar. | P-02 y P-03 como gates obligatorios. |
| Despliegue sin preproducci├│n | Un fallo de configuraci├│n o migraci├│n aparece directamente en producci├│n. | E2.5 y P-12: staging equivalente en configuraci├│n, con base/credenciales/notificaciones aisladas, smoke tests y rollback probado antes del piloto. |
| Cambios locales mezclados en stashes y archivos sin seguimiento | Aplicar un bloque puede introducir c├│digo no revisado, secretos o regresiones. | P-09: revisi├│n archivo por archivo; nada de `git stash pop` masivo. |
| Cobro manual sin registro en el sistema | P├®rdida de trazabilidad, disputas y suspensiones inconsistentes. | P-04: planilla controlada con datos m├¡nimos, verificaci├│n contra comprobante/extracto y revisi├│n mensual de vencimientos; runbook de suspensi├│n. |
| Suspensi├│n no validada de punta a punta | Un negocio suspendido podr├¡a conservar acceso o dejar datos inaccesibles. | P-05: prueba controlada de acceso p├║blico, admin, enlaces y dominios. |
| Dominios personalizados sin proceso de certificados | Un dominio puede quedar inactivo o con TLS inv├ílido. | Mantenerlos fuera de V1; E6 con ACME/ALB y rollback probado. |
| Rate limiting en memoria | L├¡mites por proceso si se escala horizontalmente. | Documentar; no escalar horizontalmente en V1 sin almacenamiento compartido. |
| Dependencia de Gemini | El chat puede fallar o degradarse por cuota, timeout o clave ausente. | P-11 y monitoreo; la reserva por wizard no depende de la IA. |
| Fallos preexistentes de Windows | Ruido en la suite local que puede ocultar regresiones. | Comparar contra baseline documentado en `AGENTS.md`. |
| Marca TurnoBot/TurnoGo inconsistente | Confusi├│n comercial y documental. | P-08 antes de material de ventas. |
| M├®tricas sin ingresos | El panel no sirve para facturaci├│n ni rentabilidad. | No prometerlo; usar m├®tricas operativas y cobro manual externo. |
| Migraci├│n o restore mal ejecutado | P├®rdida de datos o corte de servicio. | Backup verificado, dry-run, base aislada y autorizaci├│n expl├¡cita. |
| Complejidad del redise├▒o admin | Regresiones de UX en flujos cr├¡ticos. | Cerrar fases con pruebas y no mezclar con el lanzamiento comercial. |

---

## 9. Funcionalidades expresamente fuera del alcance de V1

Las siguientes capacidades **no** son requisitos de lanzamiento. Pueden evaluarse
despu├®s de V1 y no deben bloquear la comercializaci├│n con dominio compartido:

- Dominios personalizados obligatorios o incluidos en el plan base.
- Emisi├│n, renovaci├│n y soporte autom├ítico de certificados TLS.
- Aprovisionamiento autom├ítico de Nginx, ALB, ingress o DNS por cliente.
- Dominios comod├¡n o resoluci├│n por sufijo.
- Pasarela de pagos, cobro autom├ítico, d├®bitos, recurrencia autom├ítica de cobros o
  conciliaci├│n bancaria. La suscripci├│n mensual con cobro manual s├¡ forma parte de
  V1; lo excluido es su automatizaci├│n.
- Facturaci├│n electr├│nica, notas de cr├®dito o gesti├│n contable dentro de TurnoBot.
  Esto **no exime** al negocio de sus obligaciones fiscales, impositivas y
  contables: se gestionan externamente con su contador o sistema fiscal
  correspondiente. TurnoBot no emite comprobantes fiscales ni calcula impuestos.
- Portal de autogesti├│n para que el cliente cambie de plan o pague.
- Suspensi├│n autom├ítica por mora.
- Aplicaciones m├│viles nativas.
- Integraciones con Google Calendar, Outlook, WhatsApp Business API u otros
  calendarios o canales no implementados.
- Anal├¡tica de ingresos, ocupaci├│n estimada o precios hist├│ricos.
- Multi-idioma, multi-moneda o multi-regi├│n.
- Row-Level Security, aislamiento por esquema o particionamiento.
- Marketplace, API p├║blica para terceros o programa de partners.
- Migraci├│n masiva desde otros sistemas.
- Suplantaci├│n de usuarios desde Superadmin.
- Automatizaci├│n de marketing, campa├▒as o recuperaci├│n de clientes inactivos m├ís
  all├í de lo ya existente en fidelizaci├│n.
- Redise├▒o completo del panel admin como condici├│n de lanzamiento.
- Cualquier cambio de infraestructura, DNS, certificados o producci├│n no autorizado
  expl├¡citamente.

---

## 10. Criterios para considerar V1 lista para comercializar

V1 se considera lista solo cuando **todos** los bloques siguientes est├ín cumplidos y
aprobados. Las notas marcadas como **no bloqueantes** son recomendaciones y no forman
parte de los criterios. Los dominios personalizados no son un criterio de V1; la
correcci├│n de sus rutas administrativas es higiene t├®cnica recomendable y no
bloqueante.

### A. Funcionalidad del negocio

- [ ] Un negocio puede solicitar el alta y ser aprobado manualmente por Superadmin.
- [ ] El owner recibe la invitaci├│n, define contrase├▒a y accede al panel.
- [ ] La p├ígina p├║blica `/b/<slug>` muestra servicios y permite reservar.
- [ ] El wizard de reserva funciona de punta a punta con email configurado.
- [ ] La agenda permite crear, cancelar, cambiar estado y reprogramar turnos.
- [ ] El sistema impide dobles turnos para el mismo recurso/profesional en el mismo
      horario, incluso con reservas concurrentes.
- [ ] Las reservas respetan la zona horaria y los horarios configurados por el negocio.
- [ ] Servicios, horarios, recursos, usuarios y roles funcionan con permisos correctos.
- [ ] El negocio puede editar sus datos b├ísicos (nombre, contacto, marca y zona
      horaria) sin intervenci├│n t├®cnica.
- [ ] Fidelizaci├│n, conocimiento y conversaciones operan sin errores cr├¡ticos.
- [ ] Las notificaciones y recordatorios se env├¡an o fallan de forma controlada.
- [ ] El chat con IA funciona con la clave configurada y existe un modo degradado
      aceptable si el proveedor falla.

### B. Panel Superadmin

- [ ] El Superadmin inicia y cierra sesi├│n por el canal de plataforma, separado de
      la autenticaci├│n de negocio.
- [ ] El login de plataforma aplica CSRF, rate limit y cookies host-only.
- [ ] El Superadmin puede listar negocios, crear solicitudes, aprobarlas, reenviar
      invitaciones y ver el detalle sin exponer datos de otro negocio.
- [ ] Solo el Superadmin puede activar, suspender y reactivar un negocio; ning├║n
      usuario de negocio puede hacerlo.
- [ ] Cada acci├│n de plataforma (aprobaci├│n, suspensi├│n, reactivaci├│n, reenv├¡o)
      queda registrada en la auditor├¡a con actor, fecha y negocio afectado.
- [ ] Existe un procedimiento documentado y probado para suspender y reactivar.

### C. Aislamiento y seguridad

- [ ] Todas las operaciones administrativas validan membres├¡a y rol.
- [ ] El `business_id` se obtiene del contexto resuelto, nunca de un formulario.
- [ ] Un usuario de un negocio no puede leer ni modificar datos de otro.
- [ ] Las pruebas de aislamiento cubren SQLite y PostgreSQL, incluyendo endpoints
      administrativos y operaciones de escritura.
- [ ] Las pruebas verifican que no se puede acceder a otro negocio cambiando IDs,
      slugs o campos de formulario.
- [ ] La autorizaci├│n por rol, el aislamiento entre tenants y CSRF se validan con una
      matriz de casos; un 401 aislado no se acepta como evidencia suficiente.
- [ ] Un host desconocido no resuelve al negocio por defecto.
- [ ] Suspender un negocio corta el acceso p├║blico y administrativo esperado.
- [ ] CSRF, cookies, sesiones, recuperaci├│n de contrase├▒a, pol├¡tica de contrase├▒as y
      rate limits est├ín validados en preproducci├│n equivalente a producci├│n, con
      base, credenciales y notificaciones aisladas, **antes** del despliegue.
- [ ] Despu├®s del despliegue en AWS se ejecutaron smoke tests de login, reserva,
      panel admin, `/health` y notificaciones.
- [ ] No hay secretos en el repositorio, logs, URLs ni archivos que se vayan a
      commitear.
- [ ] Los stashes y archivos locales mezclados fueron revisados selectivamente y
      ninguno se aplic├│ en bloque.
- [ ] No hay vulnerabilidades cr├¡ticas conocidas en las dependencias instaladas.

### D. Datos, AWS y operaci├│n

- [ ] La preproducci├│n AWS usa una base de datos independiente, credenciales
      independientes y notificaciones aisladas; no se conecta a la base de
      producci├│n ni env├¡a correos reales a clientes.
- [ ] Producci├│n usa PostgreSQL con `DATABASE_URL` v├ílida y sin fallback silencioso
      a SQLite.
- [ ] Las migraciones PostgreSQL se aplicaron con backup previo, dry-run y
      verificaci├│n; no se aplicaron desde el arranque.
- [ ] El rollback de migraciones fue probado en preproducci├│n y est├í documentado.
- [ ] Existe un backup reciente y una prueba de restore en base aislada.
- [ ] Est├ín definidos RPO, RTO, retenci├│n y frecuencia de simulacros de restore.
- [ ] Existe al menos una copia del backup fuera del servidor de aplicaci├│n.
- [ ] `/health` responde correctamente y el monitoreo de disponibilidad est├í activo
      en AWS.
- [ ] La aplicaci├│n se despliega en AWS con un servicio administrado, reinicio
      autom├ítico y logs accesibles.
- [ ] Las variables y secretos de producci├│n est├ín configurados fuera del repositorio
      y verificados en el entorno desplegado.
- [ ] El SMTP saliente funciona desde AWS y se verific├│ una entrega real a una
      direcci├│n de prueba controlada, no a clientes.
- [ ] Los recordatorios y reintentos de notificaciones tienen un proceso operativo.
- [ ] Se verific├│ el manejo de rebotes y el opt-out de notificaciones.
- [ ] Existe runbook de suspensi├│n, reactivaci├│n, incidentes y rollback.
- [ ] El rollback de aplicaci├│n fue probado en preproducci├│n y est├í documentado.
- [ ] Los servidores y entornos (AWS actual, OCI anterior) est├ín identificados y no
      se confunden en operaciones.

### E. Calidad y validaci├│n

- [ ] La suite completa se ejecut├│ en Python 3.12 + PostgreSQL 16 sobre el HEAD
      candidato y no introdujo fallos nuevos.
- [ ] CI est├í verde en el commit candidato exacto, no solo en una ejecuci├│n local.
- [ ] `ruff check`, `ruff format --check` y `mypy app.py` pasan o tienen hallazgos
      justificados y aceptados.
- [ ] Los ├║nicos fallos son los preexistentes documentados (symlink en Windows y
      flakiness de concurrencia conocido).
- [ ] La documentaci├│n de estado y el `Registro de avances` est├ín actualizados.
- [ ] El Registro distingue con claridad avance local, commit, integraci├│n en GitHub
      y validaci├│n por CI.

> **Nota (no bloqueante):** la correcci├│n de las rutas administrativas de dominios
> es **recomendable** para evitar un enlace roto, pero **no es un criterio de
> lanzamiento** con dominio compartido. Si se integra, debe validarse con la matriz
> completa de autorizaci├│n, aislamiento entre tenants y CSRF; un 401 aislado no es
> evidencia suficiente.

### F. Modelo comercial y soporte

- [ ] Precio, moneda, ciclo mensual, condiciones y alcance del plan est├ín definidos.
- [ ] La suscripci├│n mensual con cobro manual est├í documentada y probada al menos una
      vez.
- [ ] No hay recurrencia autom├ítica ni pasarela de pagos en V1.
- [ ] Las obligaciones fiscales del negocio se gestionan externamente; TurnoBot no
      emite comprobantes fiscales ni calcula impuestos.
- [ ] Existe un registro de pagos y vencimientos (interno o planilla externa
      controlada) con negocio, per├¡odo, importe, moneda, medio, referencia, fecha de
      pago, vencimiento, estado y responsable de registro/verificaci├│n.
- [ ] Un responsable designado verifica los pagos contra el comprobante o extracto y
      revisa los vencimientos al menos una vez por mes.
- [ ] El Superadmin puede suspender y reactivar siguiendo un procedimiento conocido.
- [ ] Hay un canal de soporte y un acuerdo de tiempos de respuesta para el piloto.
- [ ] T├®rminos, privacidad y tratamiento de datos est├ín revisados para el alta.
- [ ] Existe manual de usuario/onboarding para el negocio.
- [ ] Existen runbooks de despliegue, backup/restore, incidentes y rollback.
- [ ] Hay responsables t├®cnicos y comerciales, canal de soporte y escalamiento
      definidos.
- [ ] El material de venta no promete dominios personalizados, pagos autom├íticos ni
      m├®tricas de ingresos.

### G. Piloto, incorporaci├│n de clientes y aprobaci├│n

- [ ] Se ejecut├│ un piloto controlado con 1 a 3 negocios reales despu├®s de
      preproducci├│n.
- [ ] Cada cliente piloto firm├│ o acept├│ t├®rminos, privacidad y tratamiento de datos.
- [ ] Se definieron criterios de ├®xito, capacitaci├│n y proceso de salida por cliente.
- [ ] No hubo incidentes cr├¡ticos de aislamiento, p├®rdida de datos ni exposici├│n de
      secretos durante el piloto.
- [ ] El feedback cr├¡tico del piloto fue resuelto o aceptado expl├¡citamente.
- [ ] Un responsable t├®cnico y un responsable comercial firmaron el go/no-go.
- [ ] El commit candidato, el estado de migraciones, el backup y el rollback fueron
      verificados antes del lanzamiento.
- [ ] La preproducci├│n AWS y los smoke tests post-despliegue pasaron antes del
      lanzamiento.

---

## Registro de avances

Actualizar este registro cada vez que se complete una etapa. No borrar filas
anteriores: agregar una nueva fila con la fecha y la evidencia concreta.

**├ümbito:** `Local` = cambio presente en un worktree, sin commit; `Sin commit` =
no integrado en git; `Integrado` = commit en GitHub; `CI` = workflow aprobado en el
commit candidato. En esta revisi├│n **no hay filas Integrado ni CI** para el HEAD
actual. La ejecuci├│n local reportada de 20 tests en Python 3.13.14 **no** equivale a
CI aprobado.

| Fecha | Etapa | ├ümbito | Estado | Evidencia / cambios | Pr├│ximo paso |
|---|---|---|---|---|---|
| 2026-11-06 | Documento | Local (`C:\Proyectos\turnobot`) | Completado local; no integrado | Creaci├│n y revisi├│n 2 de `docs/product/PLAN_TURNOBOT_V1.md` con auditor├¡a del repositorio y hoja de ruta. El documento **no est├í** en `turnobot-main-clean`. Sin commit ni push. | Trasladar e integrar en `turnobot-main-clean` con autorizaci├│n expl├¡cita. |
| 2026-11-06 | E0. L├¡nea base | Local | En curso | Revisi├│n de `README.md`, `AGENTS.md`, `docs/`, rutas, servicios, base y git. HEAD local `e57ad6b`; `origin/main` `4bb5a1a`. Venv local Python 3.13.14; CI fija 3.12. `TURNOBOT_PG_URL` definida; suite completa no ejecutada en esta revisi├│n. | Cerrar E1 y ejecutar E2 en Python 3.12 + PostgreSQL 16. |
| 2026-11-06 | E1. Higiene de rutas de dominios | Local / sin commit / sin CI | En curso | El commit `4bb5a1a` no registraba las rutas. En `turnobot-main-clean` hay correcci├│n local en tres archivos: `routes/__init__.py` (registra `business_domains.register(app)`), `tests/test_standalone_create_app.py` (mapa 136 rules / 103 endpoints) y `tests/test_business_domain_route_registration.py` (nuevo; 18 rutas y 401). Seg├║n la ├║ltima ejecuci├│n local: **20 tests OK** con Python 3.13.14; `python -m ruff format --check --exclude ".venv*" .` ÔåÆ **208 files already formatted**; `python -m ruff check --exclude ".venv*" .` ÔåÆ **All checks passed!**. Esa evidencia **no sustituye** la suite completa ni CI. **Falta:** PostgreSQL, Python 3.12, CI, commit/integraci├│n y matriz completa de autorizaci├│n/aislamiento/CSRF; el 401 aislado no alcanza. Es higiene t├®cnica recomendable, no activaci├│n comercial de dominios. | Validar en el entorno soportado y commitear con autorizaci├│n. |
| 2026-11-06 | E2. Validaci├│n integral | Local | Pendiente | Sin ejecuci├│n completa en Python 3.12 + PostgreSQL para el HEAD actual. La corrida local reportada es parcial (20 tests) y no sustituye la suite ni CI. | Ejecutar suite, lint, formato, typecheck y migraciones en entorno aislado; obtener CI verde. |
| 2026-11-06 | E2.5. Preproducci├│n AWS | Local | Pendiente | No se despleg├│ en preproducci├│n ni se accedi├│ a AWS en esta revisi├│n. | Desplegar en staging, smoke tests, validar secretos/SMTP/health y probar rollback. |
| 2026-11-06 | E3. Modelo comercial | Local | Pendiente | No se encontr├│ c├│digo de suscripci├│n, planes, pagos ni facturaci├│n. El alta y la aprobaci├│n manual existen; el cobro manual no tiene registro en la app. El modelo es suscripci├│n mensual con cobro manual, sin recurrencia autom├ítica ni pasarela. | Definir precio, proceso de cobro y runbook de suspensi├│n. |
| 2026-11-06 | E4. Piloto | Local | Pendiente | Depende de E2, E2.5 y E3. | Seleccionar negocios piloto y criterios de salida. |
| 2026-11-06 | E5. Lanzamiento V1 | Local | Pendiente | Depende del cumplimiento de la secci├│n 10 y de E2.5/E4. | Preparar checklist de go/no-go. |
| 2026-11-06 | E6. Dominios personalizados | Local | Futuro | Fuera del alcance inicial de lanzamiento. La implementaci├│n existente est├í parcial y sin ingress/TLS automatizado. | Reevaluar despu├®s de V1 con un proceso operativo validado. |
