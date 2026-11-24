# Plan TurnoBot V1

> Hoja de ruta oficial de TurnoBot V1. Este documento es la referencia para futuras
> sesiones de desarrollo y para cualquier agente de IA que trabaje en el repositorio.
>
> **Última actualización:** 2026-11-24 (revisión 3, recuperación y endurecimiento de backups/S3)
>
> **Ubicación actual:** `C:\Proyectos\turnobot\docs\product\PLAN_TURNOBOT_V1.md`,
> rama `recovery/local-improvements-20261124`. Los cambios de esta revisión están
> sin commit y **no** integrados en `main`.
>
> **Estado general:** V1 **no está lista para comercializar**. Hay funcionalidades
> existentes y otras parcialmente implementadas; el lanzamiento depende de cerrar
> los pendientes de validación, preproducción y proceso comercial. La corrección de
> rutas de dominios es higiene técnica no bloqueante.
>
> **Naturaleza del documento:** planificación y estado. No modifica código funcional,
> no agrega dependencias y no autoriza despliegues, migraciones ni cambios en producción.

## Convenciones de estado

| Etiqueta | Significado |
|---|---|
| `[EXISTENTE]` | Implementado y con evidencia en código, pruebas o documentación verificable. No implica que esté validado en todos los entornos. |
| `[PARCIAL]` | Hay implementación, pero falta una parte funcional, su exposición, una prueba o una validación crítica. |
| `[PENDIENTE]` | Requisito de V1 que todavía no está implementado o no tiene proceso operativo definido. |
| `[EN CURSO]` | Trabajo iniciado, con avance local o parcial, pero sin cumplir todavía su criterio de salida. |
| `[FUTURO]` | Fuera del alcance de V1; se documenta para no confundirlo con deuda de lanzamiento. |

## Base de evidencia y límites de esta revisión

Esta hoja de ruta se apoya en la documentación y el código del repositorio, no en
suposiciones de producto:

- `README.md`, `AGENTS.md`, `DEPLOYMENT.md`, `CUTOVER_PROCEDURE.md`.
- `docs/custom-domains.md`, `docs/admin_dashboard_audit.md`,
  `docs/TEST_SUITE_INVENTORY.md`.
- `routes/`, `services/`, `database/`, `migrations/`, `migrations_pg/`,
  `templates/` y `tests/`.
- `git log`, `git status`, `git stash list` y ramas locales.

Límites explícitos:

- **La suite integral verificada más reciente es anterior a estos cambios.** El run
  `45396217542` del workflow `tests.yml` sobre
  `2e2128bbff74ed8ba66652512d38fb032d4d099a` registró: Ruff check/format OK,
  MyPy incremental OK, **1.817 passed / 2 skipped** en `tests/`, **145 passed** en
  `tests_pg/`, 35 subtests passed, T4-T6 3 passed, `visual-tests` success y
  `sonarqube_analysis` success. No se extrapola ese resultado a los cambios
  locales pendientes de esta revisión.
- **La validación integral de los cambios actuales está pendiente.** Los cambios
  de endurecimiento están sin commit en la rama de recuperación. Windows se usa
  solo para validaciones puntuales (Ruff, formato, MyPy y grupos acotados de
  tests); la validación integral debe ejecutarse en GitHub Actions con Python
  3.12 y PostgreSQL 16 tras autorización de commit/push.
- **Existe un respaldo de cambios locales mezclados.** Hay tres entradas de
  `git stash` (`WIP: preserve local changes before rebase`,
  `WIP: preserve TurnoBot changes before test-8-workers` y `fix-invitacion`) y
  archivos sin seguimiento (helpers `.ps1`/`.bat`, `.env.bak`, `tests/visual/`,
  scripts sueltos, `.cleanup_backup/`, entre otros). Mezclan cambios de distintas
  ramas y épocas, incluidos archivos potencialmente sensibles y experimentos.
  **No deben aplicarse en bloque, commitearse ni usarse como base de V1.**
- **Corrección local de rutas de dominios (no integrada).** En
  `C:\Proyectos\turnobot-main-clean` existe una corrección local sin commit en tres
  archivos: `routes/__init__.py` (importa e invoca `business_domains.register(app)`),
  `tests/test_standalone_create_app.py` (actualiza el mapa a 136 rules / 103
  endpoints y valida las reglas de dominios) y
  `tests/test_business_domain_route_registration.py` (nuevo; valida el registro de
  las 18 rutas y el rechazo 401 sin autorización). Según la última ejecución local,
  pasaron **20 tests** con **Python 3.13.14**;
  `python -m ruff format --check --exclude ".venv*" .` informó **208 files already
  formatted** y `python -m ruff check --exclude ".venv*" .` informó **All checks
  passed!**. Esa evidencia de lint/formato **no sustituye** la suite completa ni CI.
  **Faltan:** validación con PostgreSQL, ejecución en Python 3.12, CI, commit e
  integración en GitHub, y la matriz completa de autorización/aislamiento/CSRF (el
  401 aislado no alcanza). El commit `4bb5a1a` no registraba estas rutas.
- **La marca no está unificada.** La landing comercial usa **TurnoBot**
  (`templates/marketing_home.html`), mientras que `README.md` y varios documentos
  heredados usan **TurnoGo**. Es una tarea de documentación y comunicación, no un
  cambio funcional de V1.

---

## 1. Visión y objetivo comercial de TurnoBot V1

### Visión

TurnoBot es una plataforma SaaS multi-tenant para negocios que trabajan con turnos,
atenciones o servicios. Su propuesta es centralizar la operación diaria en un solo
lugar: página pública de reservas, agenda, gestión de servicios y recursos, atención
por chat con IA, fidelización y métricas operativas.

### Objetivo comercial de V1

Poner a disposición de negocios reales una versión **estable, aislada por negocio y
operable con procesos manuales controlados**, que permita:

1. Dar de alta un negocio con aprobación manual.
2. Publicar su página de reservas en el dominio compartido de TurnoBot.
3. Recibir y gestionar reservas, cancelaciones y reprogramaciones.
4. Usar el panel administrativo para operar el negocio.
5. Permitir que el Superadmin active o suspenda el servicio.
6. Cobrar una suscripción mensual por un proceso manual, sin pasarela de pagos.

### Perfil inicial

Negocios pequeños y medianos con agenda de turnos: barberías, centros de estética,
consultorios, clases, talleres y servicios profesionales. La landing comercial ya
apunta a esos rubros y el alta se solicita por WhatsApp o por el formulario público
`/registro`.

### Métricas de éxito propuestas para V1

Son objetivos de validación comercial, no afirmaciones de resultado:

- Tiempo de alta asistida menor a un día hábil una vez aprobado el negocio.
- Un negocio piloto puede operar reservas y gestión diaria sin intervención técnica.
- Cero incidentes críticos de aislamiento entre negocios durante el piloto.
- Cobro manual y suspensión/activación ejecutables sin tocar la base de datos.
- El negocio puede volver a operar tras una suspensión sin pérdida de datos.

---

## 2. Funcionalidades para los negocios

### 2.1 Página pública y reservas

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Landing pública por negocio en `/b/<slug>` | `[EXISTENTE]` | `routes/public.py`, `templates/landing.html`; el negocio se resuelve por slug. |
| Página comercial de la plataforma en `/` | `[EXISTENTE]` | `templates/marketing_home.html`; incluye CTA por WhatsApp. |
| Wizard de reserva en 4 pasos | `[EXISTENTE]` | `routes/public.py`: servicio, fecha, datos y confirmación. |
| Reserva, cancelación y reprogramación pública | `[EXISTENTE]` | `/api/reservar`, `/api/cancelar`, `/api/reprogramar`; gestión con token. |
| Límite anti-abuso por contacto | `[EXISTENTE]` | Se aplica dentro de la transacción de reserva; cubierto por la suite documentada. |
| Protección honeypot, timestamp e idempotencia en el wizard | `[EXISTENTE]` | `routes/public.py` y pruebas asociadas. |
| Disponibilidad por fecha y recursos | `[EXISTENTE]` | `/api/disponibilidad/<fecha>`, `/api/recursos`. |
| Página para gestionar un turno con token | `[EXISTENTE]` | `/b/<slug>/turno/<token>?id=<id>`; `templates/gestionar_turno.html`. |

### 2.2 Recepcionista con IA

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Chat con Google Gemini | `[EXISTENTE]` | `services/ai.py`, `/chat`; requiere `GEMINI_API_KEY`. |
| Herramientas de disponibilidad, recursos, reserva, búsqueda, cancelación, reprogramación y escalado humano | `[EXISTENTE]` | Siete herramientas documentadas en `README.md`. |
| Confirmación de acciones mutantes y límites de iteraciones/historial | `[EXISTENTE]` | Guardas en el servicio de IA y pruebas documentadas. |
| Handoff a atención humana y moderación de conversaciones | `[EXISTENTE]` | Panel de conversaciones y estados. |
| Dependencia de un proveedor externo de IA | `[PARCIAL]` | Si falta la clave o el proveedor falla, la disponibilidad del chat no está garantizada; debe existir un modo degradado explícito para V1. |

### 2.3 Panel administrativo del negocio

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Login, logout, invitaciones y recuperación de contraseña | `[EXISTENTE]` | `routes/auth.py`, `routes/auth_public.py`, `routes/invitation.py`. |
| Roles owner/admin/staff y gestión de usuarios | `[EXISTENTE]` | `services/memberships.py`, `routes/admin.py`. |
| Agenda y gestión de turnos | `[EXISTENTE]` | Cancelar, cambiar estado, reprogramar, crear turno manual. |
| Servicios y horarios semanales | `[EXISTENTE]` | CRUD de servicios y guardado de horarios. |
| Recursos reservables | `[EXISTENTE]` | Crear, activar/desactivar y asignar a turnos. |
| Fidelización y recompensas | `[EXISTENTE]` | Puntos, ledger, configuración, catálogo y canjes. |
| Base de conocimiento | `[EXISTENTE]` | CRUD y búsqueda full-text; PostgreSQL `tsvector`/GIN y fallback SQLite FTS5. |
| Conversaciones y escalado humano | `[EXISTENTE]` | Listado, detalle, resolución y cambio de estado. |
| Métricas operativas | `[EXISTENTE]` | `/admin/metricas`: período anterior, servicios top, demanda por día/hora, clientes, chat y fidelización. |
| Analítica de IA y oportunidades | `[EXISTENTE]` | `/admin/inteligencia`, `/admin/inteligencia/oportunidades`. |
| Rediseño integral del panel | `[PARCIAL]` | `docs/admin_dashboard_audit.md` marca solo la auditoría como cerrada; el plan por fases (dashboard, agenda, clientes, servicios, empleados, estadísticas, notificaciones, integración) no está cerrado. La rama `feature/admin-panel-redesign` ya incorpora nueva UI, sidebar y tokens. |
| Métricas de ingresos, facturación u ocupación estimada | `[FUTURO]` | El propio panel declara que no hay ingresos ni ocupación estimada porque no existe precio histórico fiable. |

### 2.4 Notificaciones y operación

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Confirmaciones y recordatorios por email | `[EXISTENTE]` | `services/notifications.py`, `scripts/send_reminders.py`; requiere SMTP configurado. |
| Reintentos de notificaciones | `[EXISTENTE]` | Política de reintentos y tablas asociadas. |
| Health check con conectividad a base | `[EXISTENTE]` | `/health`, `scripts/check_health.py`. |
| Backups PostgreSQL y verificación del dump | `[EXISTENTE]` | `scripts/backup_postgresql.py`, `scripts/verify_backup_postgresql.py`. |
| Restauración segura en base aislada | `[EXISTENTE]` | `scripts/restore_postgresql.py`; rechaza el destino de producción. |
| Panel de dominio personalizado | `[PARCIAL]` | Ver sección 5: hay modelo, resolver, plantilla y pruebas de datos. La corrección de rutas es higiene técnica local en `turnobot-main-clean`. La activación comercial de dominios propios es `[FUTURO]` y queda **fuera de V1**. |

---

## 3. Funcionalidades del panel Superadmin

El Superadmin es una identidad de plataforma separada de los usuarios de negocio.
Vive en `platform_users` y opera con prefijos `platform_*`; no es tenant-scoped por
diseño (`services/platform.py`).

### 3.1 Funcionalidades existentes

| Funcionalidad | Estado | Evidencia / notas |
|---|---|---|
| Login y logout separados de la autenticación de negocio | `[EXISTENTE]` | `/superadmin/login`, `/superadmin/logout`; sesión de plataforma y token persistente. |
| Protección CSRF y rate limit de login | `[EXISTENTE]` | `valid_csrf_token`, `is_login_request_allowed`. |
| Listado de negocios con estado | `[EXISTENTE]` | `list_businesses_for_platform`, `templates/superadmin.html`. |
| Crear solicitud de alta de negocio | `[EXISTENTE]` | Crea negocio pendiente y owner pendiente; no genera invitación todavía. |
| Aprobar solicitud y generar invitación del owner | `[EXISTENTE]` | `approve_business`; el negocio queda activo y el owner define contraseña por invitación. |
| Reenviar invitación | `[EXISTENTE]` | Revoca invitaciones activas y genera una nueva. |
| Suspender y reactivar negocio | `[EXISTENTE]` | `set_business_active`; el resolver público exige `active` y los dominios personalizados exigen negocio activo. |
| Ver detalle del negocio, configuración, miembros e invitaciones | `[EXISTENTE]` | `superadmin_negocios_detalle`. |
| Auditoría de acciones de plataforma | `[EXISTENTE]` | `audit_log`, `/superadmin/auditoria`, `log_platform_action`. |
| Bootstrap de Superadmin por CLI | `[EXISTENTE]` | `scripts/create_superadmin.py`. |

### 3.2 Funcionalidades parciales o pendientes

| Funcionalidad | Estado | Notas |
|---|---|---|
| Estado comercial del negocio (plan, precio, vencimiento, estado de cobro) | `[PENDIENTE]` | No hay modelo de suscripción ni campos de facturación en el esquema ni en el panel. |
| Historial de pagos y comprobantes | `[PENDIENTE]` | El cobro es manual y fuera de la aplicación; no hay registro dentro del panel. |
| Suspensión automática por falta de pago | `[FUTURO]` | V1 usa suspensión manual desde Superadmin. |
| Motivo estructurado de suspensión | `[PENDIENTE]` | Hoy la acción se registra en auditoría, pero no hay un campo de motivo comercial normalizado. |
| Métricas de uso por negocio para facturación o salud comercial | `[FUTURO]` | No es requisito de V1; el panel de negocio ya tiene métricas operativas. |
| Suplantación o impersonación controlada | `[FUTURO]` | No está implementada; si se evaluara, requeriría auditoría y límites estrictos. |
| Gestión de dominios personalizados desde Superadmin | `[FUTURO]` | La gestión actual de dominios es del owner; no hay consola de plataforma para aprovisionar ingress/TLS. |

---

## 4. Arquitectura multi-tenant y aislamiento de datos

### 4.1 Modelo actual

`[EXISTENTE]` La aplicación es multi-tenant por `business_id`:

- El tenant se resuelve desde el slug de la URL (`/b/<slug>`) o desde un dominio
  personalizado verificado por Host.
- `g.current_business` y `get_current_business_id()` son la fuente del negocio
  actual; las rutas administrativas no aceptan `business_id` desde formularios
  para decidir el tenant.
- Las consultas de dominio usan funciones `*_scoped` que reciben `business_id` y
  filtran por él (`database/database.py`).
- La autenticación de negocio usa membresías (`business_users`); owner/admin/staff
  se validan contra el negocio resuelto.
- La autenticación de plataforma es independiente (`platform_users`) y no se mezcla
  con membresías de negocio.
- El esquema PostgreSQL declara `business_id` con claves foráneas y restricciones
  por tabla (`migrations_pg/001_initial_schema.sql` y `003_business_domains.sql`).

### 4.2 Controles de aislamiento

`[EXISTENTE]`

- `_require_admin_membership()` y `membership_required(...)` verifican sesión y rol
  antes de operar (`routes/admin.py`).
- La gestión de dominios exige rol `owner` y obtiene el negocio desde el contexto,
  nunca desde el formulario (`routes/business_domains.py`).
- Los dominios personalizados solo resuelven si están verificados, activos, con TLS
  validado y asociados a un negocio activo (`database/business_domains.py`).
- Un host desconocido no cae al negocio `id=1`; el Host de plataforma solo se usa
  para `localhost`/`127.0.0.1` y el resto responde 404 (`application/tenant.py`).
- `PUBLIC_BASE_URL` es el origen de confianza para enlaces absolutos; no se
  construyen enlaces desde el Host entrante (`application/public_urls.py`).
- Las cookies de sesión son host-only: un login en un hostname no se comparte con
  otro.

### 4.3 Riesgos y validaciones pendientes

- `[PARCIAL]` La suite de aislamiento y multi-tenant está documentada, pero el HEAD
  actual no tiene una ejecución completa registrada en Python 3.12 + PostgreSQL.
- `[EN CURSO]` Las rutas administrativas de dominios ya tienen una corrección local
  sin commit en `turnobot-main-clean` (tres archivos, 20 tests reportados en Python
  3.13.14). Falta validarlas con PostgreSQL, Python 3.12 y CI, y cubrir de forma
  explícita autorización, aislamiento y CSRF en el entorno soportado. Esta
  corrección es **higiene técnica**: no convierte la activación de dominios propios
  en requisito comercial de V1.
- `[PENDIENTE]` Validar el comportamiento de suspensión sobre sesiones ya abiertas,
  enlaces públicos y dominios personalizados en un entorno real.
- `[FUTURO]` Row-Level Security (RLS) de PostgreSQL, aislamiento por esquema o
  particionamiento no forman parte de V1; el aislamiento actual es de aplicación.
- `[PARCIAL]` El rate limiting es en memoria y por proceso. Si se escala
  horizontalmente, cada instancia tiene su propio estado; V1 no depende de escalado
  horizontal, pero la limitación debe quedar documentada en operaciones.

---

## 5. Modalidades de publicación

### 5.1 Dominio compartido como opción inicial

`[EXISTENTE]` Es la modalidad de lanzamiento de V1.

- Cada negocio se publica en `PUBLIC_BASE_URL/b/<slug>`.
- La landing comercial de la plataforma queda en `/`.
- El negocio por defecto (`id=1`) puede resolverse en `/` cuando corresponde.
- No requiere DNS propio, certificado propio ni cambios de ingress por cliente.
- Los enlaces de reserva, invitación y gestión usan el origen público configurado.
- Limitación aceptada: la URL contiene el slug de TurnoBot y no la marca del cliente.

**Decisión de V1:** el dominio compartido es suficiente para comercializar. Los
dominios personalizados **no son un requisito de lanzamiento ni se ofrecen en el
plan base**. La corrección de las rutas administrativas es solo higiene técnica para
evitar un enlace roto; no habilita la activación comercial de dominios propios.

### 5.2 Dominios personalizados como funcionalidad futura

**Estado:** la función comercial es `[FUTURO]`; la corrección de las rutas
administrativas es `[PARCIAL]` y se trata como higiene técnica no bloqueante.

Hay una implementación adelantada, pero la activación comercial **no está lista
para lanzamiento**:

- `[EXISTENTE]` Tabla `business_domains` y migraciones:
  - SQLite: `migrations/025_business_domains.sql`.
  - PostgreSQL: `migrations_pg/003_business_domains.sql`.
- `[EXISTENTE]` Resolución de tenant por Host en `application/tenant.py`.
- `[EXISTENTE]` Verificación de propiedad por registro TXT y comprobación TLS en
  `application/business_domains.py`.
- `[EXISTENTE]` Panel de owner en `templates/business_domains.html`, con acciones
  para agregar, verificar, rotar token, comprobar TLS, activar, desactivar, revocar
  y elegir dominio principal.
- `[EXISTENTE]` Pruebas de repositorio y de resolución en
  `tests/test_business_domains.py`, `tests/test_business_domain_routes.py` y
  `tests_pg/test_business_domains_live.py`.
- `[EN CURSO]` **Corrección de rutas administrativas de dominios.**
  `routes/business_domains.py` define las vistas y una función `register(app)`.
  El commit `4bb5a1a` **no** importaba ni invocaba ese registro, por lo que la
  pantalla enlazada desde `templates/admin.html` no quedaba expuesta. En
  `turnobot-main-clean` hay una **corrección local sin commit** en tres archivos:
  `routes/__init__.py` (registra `business_domains.register(app)`),
  `tests/test_standalone_create_app.py` (actualiza el mapa de rutas y valida las
  reglas de dominios) y `tests/test_business_domain_route_registration.py` (nuevo;
  valida las 18 rutas y el rechazo 401). Según la última ejecución local en
  `turnobot-main-clean`, pasaron **20 tests** con **Python 3.13.14**;
  `python -m ruff format --check --exclude ".venv*" .` informó **208 files already
  formatted** y `python -m ruff check --exclude ".venv*" .` informó **All checks
  passed!**. Esa evidencia de lint/formato **no sustituye** la suite completa ni CI.
  **Faltan:** validación PostgreSQL, Python 3.12, CI, commit e integración.
- `[PENDIENTE]` **Matriz completa de seguridad de rutas.** La prueba local de 401
  solo verifica el rechazo sin sesión; **no** demuestra autorización por rol
  (owner/admin/staff), aislamiento entre negocios, CSRF, rutas con y sin slug ni
  errores de registro. Esa matriz debe ejecutarse en el entorno soportado antes de
  declarar la validación terminada.
- `[FUTURO]` Emisión y renovación automática de certificados, aprovisionamiento de
  Nginx/ingress, allowlist de hostnames y automatización de DNS.
- `[FUTURO]` Dominios comodín, resolución por sufijo o múltiples niveles no están
  soportados.

**Decisión de V1:** los dominios personalizados quedan **fuera del alcance inicial
de lanzamiento**. No se ofrecen como parte del plan base ni se activan en clientes
reales hasta que exista un proceso operativo de certificados e ingress validado.
La corrección de rutas administrativas es **higiene técnica no bloqueante**: se hace
para que el panel no tenga un enlace roto, no para habilitar la función comercial en
V1.

### 5.3 Nginx y Let's Encrypt como alternativa técnica prevista

`[FUTURO]` Para una etapa posterior a V1, la documentación de dominios contempla dos
alternativas de ingress:

1. **AWS ALB + ACM** (evaluar costo, validación DNS y cambios de entrada).
2. **Nginx en EC2 + ACME operado por el equipo**, con Let's Encrypt como opción
   prevista.

La alternativa Nginx + Let's Encrypt requeriría:

- Emisión, renovación, recarga y revocación de certificados.
- Límites de emisión y manejo de fallos de ACME.
- Allowlist de hostnames provisionados.
- Terminación TLS por SNI y conservación controlada del Host autorizado.
- Reemplazo de cabeceras reenviadas del cliente y `TRUSTED_PROXY_COUNT` correcto.
- Proceso de verificación TLS desde el panel después de instalar el certificado.

`docs/custom-domains.md` aclara que la aplicación **todavía no automatiza** la
emisión/renovación de certificados ni provisiona Nginx, y que los dominios reales no
deben activarse hasta que exista y se valide ese proceso operativo.

---

## 6. Modelo comercial inicial

### 6.1 Alta manual

`[EXISTENTE]` el flujo técnico; `[PARCIAL]` el modelo comercial asistido.

Flujo actual:

1. El interesado contacta por WhatsApp desde la landing o completa `/registro`.
2. Se crea un negocio en estado **pendiente** (`active=0`, `pending=1`) con un owner
   pendiente.
3. El Superadmin revisa la solicitud y la **aprueba manualmente**.
4. La aprobación activa el negocio y genera una invitación única para que el owner
   defina su contraseña.
5. El owner acepta la invitación y accede al panel.

Notas:

- El formulario público no activa el negocio por sí solo.
- El Superadmin también puede crear una solicitud desde `/superadmin/negocios/crear`.
- La aprobación, la activación y la suspensión son **exclusivas del Superadmin**;
  ningún owner/admin/staff puede activar su propio negocio ni cambiar su estado
  comercial.
- El alta y la aprobación siguen siendo **manuales**; no hay onboarding automático
  ni pago previo.

### 6.2 Suscripción mensual

`[PENDIENTE]` Es el modelo comercial definido para V1, pero **no está implementado**.

El modelo es una **suscripción mensual con cobro manual**: se factura y cobra por un
canal externo, sin recurrencia automática y sin pasarela de pagos integrada. La
"recurrencia" existe solo a nivel comercial y operativo manual; no hay débitos
automáticos ni cobros programados por el sistema.

- No hay tabla de planes, suscripciones, precios, moneda, ciclo de facturación ni
  vencimientos.
- No hay estados comerciales (`trial`, `activo`, `moroso`, `cancelado`) en el
  esquema.
- No hay prorrateo, cambios de plan ni autogestión de la suscripción.
- La definición de precio, moneda, impuestos y condiciones queda fuera de este
  documento y debe aprobarse antes del lanzamiento.

### 6.3 Cobro manual inicialmente

`[PENDIENTE]` V1 no integra pasarela de pagos ni recurrencia automática.

- La suscripción es mensual, pero el cobro se ejecuta y registra manualmente; no hay
  débito automático ni cobros programados.
- El cobro lo ejecuta el responsable comercial designado por TurnoBot por un canal
  externo (transferencia, efectivo u otro medio acordado).
- No hay registro automático del pago dentro de TurnoBot.
- No hay conciliación automática dentro de TurnoBot, ni generación de comprobantes,
  ni bloqueo automático por falta de pago.
- La emisión de comprobantes fiscales y el cumplimiento impositivo quedan fuera de
  TurnoBot; el negocio los gestiona con su contador o sistema externo.
- Cualquier integración de pagos (Mercado Pago, Stripe u otra) es `[FUTURO]` y no
  debe agregarse como dependencia de V1 sin una decisión explícita.

**Registro inicial de pagos:** puede llevarse **fuera de TurnoBot** en una planilla
controlada (por ejemplo, una hoja de cálculo con acceso restringido y respaldo).
Datos mínimos por fila:

- negocio y `business_id` o slug;
- período cubierto (mes/año) y fecha de vencimiento;
- importe y moneda;
- fecha de pago y medio/canal (transferencia, efectivo u otro);
- referencia o comprobante (número de operación, enlace o archivo adjunto);
- estado (`pendiente`, `pagado`, `vencido`, `excepcional`);
- quién registró y quién verificó;
- observaciones.

**Verificación:** un responsable designado (Superadmin o responsable comercial
autorizado) contrasta cada pago con el extracto bancario o comprobante y marca la
fila como verificada. Cuando sea posible, quien cobra no debe ser la única persona
que verifica.

**Vencimientos:** se revisan al menos una vez por mes, antes de la fecha de
vencimiento. Si no hay pago, se marca `vencido`, se avisa al cliente y se aplica el
runbook comercial. La suspensión es una decisión manual del Superadmin; no es
automática.

### 6.4 Control de activación y suspensión desde Superadmin

`[EXISTENTE]` Es el mecanismo comercial-operativo de V1 y es **exclusivo del
Superadmin**.

- El Superadmin puede **aprobar**, **suspender** y **reactivar** un negocio.
- Ningún usuario de negocio (owner/admin/staff) puede activar, suspender ni cambiar
  el estado comercial por ninguna vía.
- La suspensión pone `active=0`; el resolver público y el resolver de dominios
  exigen negocio activo.
- Las acciones quedan registradas en la auditoría de plataforma.
- El owner puede ser reinvitado si todavía no aceptó la invitación.

Pendiente para que el control sea comercialmente completo:

- `[PENDIENTE]` Vincular operativamente el estado del negocio con el estado de la
  suscripción y del pago manual, aunque el registro sea externo.
- `[PENDIENTE]` Definir un motivo de suspensión y un procedimiento de aviso al
  cliente.
- `[PENDIENTE]` Validar en un entorno real que la suspensión corta el acceso público
  y administrativo de forma inmediata y sin efectos laterales.

---

## 7. Etapas de desarrollo y validación

Las etapas son secuenciales para el lanzamiento, salvo E1 (recomendable y no
bloqueante con dominio compartido) y las que se indiquen como paralelas o posteriores
a V1. El estado refleja la auditoría del 2026-11-06 y la revisión 2.

| Etapa | Objetivo | Entregables | Criterio de salida | Estado |
|---|---|---|---|---|
| E0. Línea base | Conocer el estado real del repositorio y separar cambios válidos de experimentos. | Auditoría de código, documentación, git status/stash, baseline de pruebas. | Riesgos y pendientes identificados; cambios mezclados aislados. | `[EN CURSO]` |
| E1. Higiene de rutas de dominios (no bloqueante) | Integrar la corrección local de las rutas `/admin/dominios` para eliminar el enlace roto, sin habilitar la activación comercial de dominios en V1. | Commit de los tres archivos, pruebas de autorización/aislamiento, CI y documentación actualizada. | Rutas expuestas solo para owner; pruebas en verde; CI aprobado; sin regresiones. No bloquea el lanzamiento con dominio compartido. | `[EN CURSO]` |
| E2. Validación integral | Ejecutar la suite completa en el entorno soportado. | Python 3.12, PostgreSQL 16, `TURNOBOT_PG_URL`, lint, formato, typecheck, suite general y PG. | 0 fallos nuevos; solo fallos preexistentes documentados y justificados. | `[PENDIENTE]` |
| E2.5. Preproducción AWS | Desplegar y validar en un entorno equivalente en configuración (no en datos) antes del piloto. | Staging en AWS con base de datos independiente, credenciales independientes, notificaciones aisladas, mismas migraciones/configuración, `/health`, logs y rollback. | Despliegue reproducible; smoke tests verdes; migraciones y rollback probados; sin conexión a la base de producción ni correos reales a clientes. | `[PENDIENTE]` |
| E3. Preparación comercial | Definir y operar el modelo de suscripción mensual con cobro manual. | Precio, condiciones, proceso de cobro, registro de pagos, runbook de suspensión. | Un cobro manual de prueba registrado; activación/suspensión ejecutables por procedimiento. | `[PENDIENTE]` |
| E4. Piloto controlado | Validar V1 con negocios reales acotados después de preproducción. | 1 a 3 negocios piloto, soporte, métricas de incidentes, feedback. | Operación estable durante el período acordado; sin incidentes críticos de aislamiento. | `[PENDIENTE]` |
| E5. Lanzamiento V1 | Comercializar con dominio compartido y procesos manuales después de preproducción y piloto. | Checklist de lanzamiento, soporte, documentación de cliente, backup/restore verificado. | Todos los criterios de la sección 10 cumplidos y aprobación explícita. | `[PENDIENTE]` |
| E6. Dominios personalizados | Habilitar dominios propios con TLS operado. | Ingress, ACME/Nginx o ALB/ACM, allowlist, automatización y soporte. | Proceso de certificados y rollback validado en producción. | `[FUTURO]` |

**Dependencias y alcance:** E2.5 es obligatoria antes de E4 (piloto) y E5
(lanzamiento); no se despliega en AWS producción sin E2 y E2.5 aprobadas. E2.5 usa
base de datos, credenciales y notificaciones independientes: no se conecta a la base
de producción ni envía correos reales a clientes. E1 es higiene técnica
**recomendable y no bloqueante** para el lanzamiento con dominio compartido. E6
queda fuera de V1.

### 7.1 Matriz de validación

| Validación | Estado actual | Qué falta |
|---|---|---|
| Python 3.12 | `[PENDIENTE]` | Hay 20 tests locales reportados en Python 3.13.14; falta ejecutar la suite completa en 3.12 según CI. |
| PostgreSQL 16 | `[PENDIENTE]` | `TURNOBOT_PG_URL` está definida, pero no se ejecutó la suite completa en esta revisión. |
| Suite general `tests/` | `[PENDIENTE]` | Ejecución completa sobre el HEAD actual. |
| Suite PostgreSQL `tests_pg/` | `[PENDIENTE]` | Ejecución completa sobre el HEAD actual. |
| Lint y formato | `[PARCIAL]` | En `turnobot-main-clean`, `python -m ruff format --check --exclude ".venv*" .` => **208 files already formatted**; `python -m ruff check --exclude ".venv*" .` => **All checks passed!**. Falta reproducirlo sobre el commit candidato; no sustituye la suite completa ni CI. |
| Typecheck | `[PENDIENTE]` | `python -m mypy app.py`. |
| Migraciones PG | `[PENDIENTE]` | `python scripts/migrate_pg.py --dry-run` y aplicación controlada en entorno aislado. |
| Preproducción AWS | `[PENDIENTE]` | Staging con base de datos y credenciales independientes y notificaciones aisladas; smoke tests, secretos/SMTP/health y rollback. Sin conexión a la base de producción ni correos reales a clientes. |
| Smoke tests post-despliegue | `[PENDIENTE]` | Verificar en el entorno desplegado que reservas, login, admin, health y notificaciones responden. |
| Backup y restore | `[PARCIAL]` | Herramientas existentes; falta prueba de restore completa en base aislada para el estado actual. |
| DNS/TLS de dominios | `[PENDIENTE]` | Probar con un hostname real, certificado válido e ingress controlado. |
| Fallos preexistentes Windows | `[EXISTENTE]` | Dos pruebas de symlink documentadas en `AGENTS.md`; no deben confundirse con regresiones nuevas. |

---

## 8. Tareas pendientes y riesgos

### 8.1 Tareas pendientes priorizadas

| ID | Prioridad | Tarea | Estado | Criterio de cierre |
|---|---|---|---|---|
| P-01 | Media (higiene, recomendable, no bloqueante de V1) | Integrar la corrección local de rutas de dominios (tres archivos en `turnobot-main-clean`) y completar la matriz de autorización/aislamiento/CSRF. | `[EN CURSO]` | Commit integrado, CI verde y PostgreSQL/Python 3.12 validados; matriz completa aprobada. El caso 401 aislado no alcanza. No bloquea el lanzamiento con dominio compartido. |
| P-02 | Alta | Ejecutar la suite completa en Python 3.12 + PostgreSQL 16 sobre el HEAD actual y obtener CI verde. | `[PENDIENTE]` | Resultado documentado, sin fallos nuevos y CI aprobado en el commit candidato. |
| P-03 | Alta | Validar migraciones 025 (SQLite) y 003 (PostgreSQL) en entorno aislado. | `[PENDIENTE]` | Dry-run, aplicación y verificación sin pérdida de datos. |
| P-04 | Alta | Definir precio, moneda, condiciones y proceso de cobro manual. | `[PENDIENTE]` | Documento comercial aprobado, planilla de pagos con datos mínimos y runbook operativo. |
| P-05 | Alta | Probar activación, suspensión y reactivación con un negocio real o aislado. | `[PENDIENTE]` | Acceso público y administrativo se corta y se restaura según lo esperado. |
| P-06 | Media | Validar DNS/TXT y TLS con un hostname real. | `[PENDIENTE]` | Verificación exitosa y fallo seguro ante certificado inválido. |
| P-07 | Media | Documentar el procedimiento de backup/restore y ejecutar una prueba de restore. | `[PARCIAL]` | Restore en base aislada verificado. |
| P-08 | Media | Unificar la marca TurnoBot/TurnoGo en documentación y textos visibles. | `[PENDIENTE]` | Sin referencias contradictorias en material de cliente. |
| P-09 | Media | Revisar selectivamente stashes y archivos locales mezclados. | `[PENDIENTE]` | Nada se aplica en bloque; secretos y experimentos descartados o aislados. |
| P-10 | Baja | Cerrar las fases restantes del rediseño del panel admin. | `[PARCIAL]` | Plan de `docs/admin_dashboard_audit.md` completo y probado. |
| P-11 | Baja | Definir modo degradado del chat si Gemini no está disponible. | `[PENDIENTE]` | La reserva pública sigue operativa sin IA. |
| P-12 | Alta | Desplegar en preproducción AWS con base/credenciales/notificaciones independientes, ejecutar smoke tests y probar rollback. | `[PENDIENTE]` | Staging equivalente en configuración validado, sin base de producción ni correos reales; rollback de aplicación y migraciones probado. |

### 8.2 Riesgos

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Rutas de dominios sin integrar (corrección local) | El owner ve un enlace roto; no puede gestionar dominios. | P-01 como higiene técnica. No bloquea V1 con dominio compartido ni habilita la función comercial. |
| Corrección local sin CI ni PostgreSQL | La corrección puede fallar en el entorno soportado. | P-01: commit, CI, Python 3.12 y PostgreSQL antes de integrar. |
| Validación incompleta en Python 3.12/PostgreSQL | Un fallo específico de producción puede aparecer recién al desplegar. | P-02 y P-03 como gates obligatorios. |
| Despliegue sin preproducción | Un fallo de configuración o migración aparece directamente en producción. | E2.5 y P-12: staging equivalente en configuración, con base/credenciales/notificaciones aisladas, smoke tests y rollback probado antes del piloto. |
| Cambios locales mezclados en stashes y archivos sin seguimiento | Aplicar un bloque puede introducir código no revisado, secretos o regresiones. | P-09: revisión archivo por archivo; nada de `git stash pop` masivo. |
| Cobro manual sin registro en el sistema | Pérdida de trazabilidad, disputas y suspensiones inconsistentes. | P-04: planilla controlada con datos mínimos, verificación contra comprobante/extracto y revisión mensual de vencimientos; runbook de suspensión. |
| Suspensión no validada de punta a punta | Un negocio suspendido podría conservar acceso o dejar datos inaccesibles. | P-05: prueba controlada de acceso público, admin, enlaces y dominios. |
| Dominios personalizados sin proceso de certificados | Un dominio puede quedar inactivo o con TLS inválido. | Mantenerlos fuera de V1; E6 con ACME/ALB y rollback probado. |
| Rate limiting en memoria | Límites por proceso si se escala horizontalmente. | Documentar; no escalar horizontalmente en V1 sin almacenamiento compartido. |
| Dependencia de Gemini | El chat puede fallar o degradarse por cuota, timeout o clave ausente. | P-11 y monitoreo; la reserva por wizard no depende de la IA. |
| Fallos preexistentes de Windows | Ruido en la suite local que puede ocultar regresiones. | Comparar contra baseline documentado en `AGENTS.md`. |
| Marca TurnoBot/TurnoGo inconsistente | Confusión comercial y documental. | P-08 antes de material de ventas. |
| Métricas sin ingresos | El panel no sirve para facturación ni rentabilidad. | No prometerlo; usar métricas operativas y cobro manual externo. |
| Migración o restore mal ejecutado | Pérdida de datos o corte de servicio. | Backup verificado, dry-run, base aislada y autorización explícita. |
| Complejidad del rediseño admin | Regresiones de UX en flujos críticos. | Cerrar fases con pruebas y no mezclar con el lanzamiento comercial. |

---

## 9. Funcionalidades expresamente fuera del alcance de V1

Las siguientes capacidades **no** son requisitos de lanzamiento. Pueden evaluarse
después de V1 y no deben bloquear la comercialización con dominio compartido:

- Dominios personalizados obligatorios o incluidos en el plan base.
- Emisión, renovación y soporte automático de certificados TLS.
- Aprovisionamiento automático de Nginx, ALB, ingress o DNS por cliente.
- Dominios comodín o resolución por sufijo.
- Pasarela de pagos, cobro automático, débitos, recurrencia automática de cobros o
  conciliación bancaria. La suscripción mensual con cobro manual sí forma parte de
  V1; lo excluido es su automatización.
- Facturación electrónica, notas de crédito o gestión contable dentro de TurnoBot.
  Esto **no exime** al negocio de sus obligaciones fiscales, impositivas y
  contables: se gestionan externamente con su contador o sistema fiscal
  correspondiente. TurnoBot no emite comprobantes fiscales ni calcula impuestos.
- Portal de autogestión para que el cliente cambie de plan o pague.
- Suspensión automática por mora.
- Aplicaciones móviles nativas.
- Integraciones con Google Calendar, Outlook, WhatsApp Business API u otros
  calendarios o canales no implementados.
- Analítica de ingresos, ocupación estimada o precios históricos.
- Multi-idioma, multi-moneda o multi-región.
- Row-Level Security, aislamiento por esquema o particionamiento.
- Marketplace, API pública para terceros o programa de partners.
- Migración masiva desde otros sistemas.
- Suplantación de usuarios desde Superadmin.
- Automatización de marketing, campañas o recuperación de clientes inactivos más
  allá de lo ya existente en fidelización.
- Rediseño completo del panel admin como condición de lanzamiento.
- Cualquier cambio de infraestructura, DNS, certificados o producción no autorizado
  explícitamente.

---

## 10. Criterios para considerar V1 lista para comercializar

V1 se considera lista solo cuando **todos** los bloques siguientes están cumplidos y
aprobados. Las notas marcadas como **no bloqueantes** son recomendaciones y no forman
parte de los criterios. Los dominios personalizados no son un criterio de V1; la
corrección de sus rutas administrativas es higiene técnica recomendable y no
bloqueante.

### A. Funcionalidad del negocio

- [ ] Un negocio puede solicitar el alta y ser aprobado manualmente por Superadmin.
- [ ] El owner recibe la invitación, define contraseña y accede al panel.
- [ ] La página pública `/b/<slug>` muestra servicios y permite reservar.
- [ ] El wizard de reserva funciona de punta a punta con email configurado.
- [ ] La agenda permite crear, cancelar, cambiar estado y reprogramar turnos.
- [ ] El sistema impide dobles turnos para el mismo recurso/profesional en el mismo
      horario, incluso con reservas concurrentes.
- [ ] Las reservas respetan la zona horaria y los horarios configurados por el negocio.
- [ ] Servicios, horarios, recursos, usuarios y roles funcionan con permisos correctos.
- [ ] El negocio puede editar sus datos básicos (nombre, contacto, marca y zona
      horaria) sin intervención técnica.
- [ ] Fidelización, conocimiento y conversaciones operan sin errores críticos.
- [ ] Las notificaciones y recordatorios se envían o fallan de forma controlada.
- [ ] El chat con IA funciona con la clave configurada y existe un modo degradado
      aceptable si el proveedor falla.

### B. Panel Superadmin

- [ ] El Superadmin inicia y cierra sesión por el canal de plataforma, separado de
      la autenticación de negocio.
- [ ] El login de plataforma aplica CSRF, rate limit y cookies host-only.
- [ ] El Superadmin puede listar negocios, crear solicitudes, aprobarlas, reenviar
      invitaciones y ver el detalle sin exponer datos de otro negocio.
- [ ] Solo el Superadmin puede activar, suspender y reactivar un negocio; ningún
      usuario de negocio puede hacerlo.
- [ ] Cada acción de plataforma (aprobación, suspensión, reactivación, reenvío)
      queda registrada en la auditoría con actor, fecha y negocio afectado.
- [ ] Existe un procedimiento documentado y probado para suspender y reactivar.

### C. Aislamiento y seguridad

- [ ] Todas las operaciones administrativas validan membresía y rol.
- [ ] El `business_id` se obtiene del contexto resuelto, nunca de un formulario.
- [ ] Un usuario de un negocio no puede leer ni modificar datos de otro.
- [ ] Las pruebas de aislamiento cubren SQLite y PostgreSQL, incluyendo endpoints
      administrativos y operaciones de escritura.
- [ ] Las pruebas verifican que no se puede acceder a otro negocio cambiando IDs,
      slugs o campos de formulario.
- [ ] La autorización por rol, el aislamiento entre tenants y CSRF se validan con una
      matriz de casos; un 401 aislado no se acepta como evidencia suficiente.
- [ ] Un host desconocido no resuelve al negocio por defecto.
- [ ] Suspender un negocio corta el acceso público y administrativo esperado.
- [ ] CSRF, cookies, sesiones, recuperación de contraseña, política de contraseñas y
      rate limits están validados en preproducción equivalente a producción, con
      base, credenciales y notificaciones aisladas, **antes** del despliegue.
- [ ] Después del despliegue en AWS se ejecutaron smoke tests de login, reserva,
      panel admin, `/health` y notificaciones.
- [ ] No hay secretos en el repositorio, logs, URLs ni archivos que se vayan a
      commitear.
- [ ] Los stashes y archivos locales mezclados fueron revisados selectivamente y
      ninguno se aplicó en bloque.
- [ ] No hay vulnerabilidades críticas conocidas en las dependencias instaladas.

### D. Datos, AWS y operación

- [ ] La preproducción AWS usa una base de datos independiente, credenciales
      independientes y notificaciones aisladas; no se conecta a la base de
      producción ni envía correos reales a clientes.
- [ ] Producción usa PostgreSQL con `DATABASE_URL` válida y sin fallback silencioso
      a SQLite.
- [ ] Las migraciones PostgreSQL se aplicaron con backup previo, dry-run y
      verificación; no se aplicaron desde el arranque.
- [ ] El rollback de migraciones fue probado en preproducción y está documentado.
- [ ] Existe un backup reciente y una prueba de restore en base aislada.
- [ ] Están definidos RPO, RTO, retención y frecuencia de simulacros de restore.
- [ ] Existe al menos una copia del backup fuera del servidor de aplicación.
- [ ] `/health` responde correctamente y el monitoreo de disponibilidad está activo
      en AWS.
- [ ] La aplicación se despliega en AWS con un servicio administrado, reinicio
      automático y logs accesibles.
- [ ] Las variables y secretos de producción están configurados fuera del repositorio
      y verificados en el entorno desplegado.
- [ ] El SMTP saliente funciona desde AWS y se verificó una entrega real a una
      dirección de prueba controlada, no a clientes.
- [ ] Los recordatorios y reintentos de notificaciones tienen un proceso operativo.
- [ ] Se verificó el manejo de rebotes y el opt-out de notificaciones.
- [ ] Existe runbook de suspensión, reactivación, incidentes y rollback.
- [ ] El rollback de aplicación fue probado en preproducción y está documentado.
- [ ] Los servidores y entornos (AWS actual, OCI anterior) están identificados y no
      se confunden en operaciones.

### E. Calidad y validación

- [ ] La suite completa se ejecutó en Python 3.12 + PostgreSQL 16 sobre el HEAD
      candidato y no introdujo fallos nuevos.
- [ ] CI está verde en el commit candidato exacto, no solo en una ejecución local.
- [ ] `ruff check`, `ruff format --check` y `mypy app.py` pasan o tienen hallazgos
      justificados y aceptados.
- [ ] Los únicos fallos son los preexistentes documentados (symlink en Windows y
      flakiness de concurrencia conocido).
- [ ] La documentación de estado y el `Registro de avances` están actualizados.
- [ ] El Registro distingue con claridad avance local, commit, integración en GitHub
      y validación por CI.

> **Nota (no bloqueante):** la corrección de las rutas administrativas de dominios
> es **recomendable** para evitar un enlace roto, pero **no es un criterio de
> lanzamiento** con dominio compartido. Si se integra, debe validarse con la matriz
> completa de autorización, aislamiento entre tenants y CSRF; un 401 aislado no es
> evidencia suficiente.

### F. Modelo comercial y soporte

- [ ] Precio, moneda, ciclo mensual, condiciones y alcance del plan están definidos.
- [ ] La suscripción mensual con cobro manual está documentada y probada al menos una
      vez.
- [ ] No hay recurrencia automática ni pasarela de pagos en V1.
- [ ] Las obligaciones fiscales del negocio se gestionan externamente; TurnoBot no
      emite comprobantes fiscales ni calcula impuestos.
- [ ] Existe un registro de pagos y vencimientos (interno o planilla externa
      controlada) con negocio, período, importe, moneda, medio, referencia, fecha de
      pago, vencimiento, estado y responsable de registro/verificación.
- [ ] Un responsable designado verifica los pagos contra el comprobante o extracto y
      revisa los vencimientos al menos una vez por mes.
- [ ] El Superadmin puede suspender y reactivar siguiendo un procedimiento conocido.
- [ ] Hay un canal de soporte y un acuerdo de tiempos de respuesta para el piloto.
- [ ] Términos, privacidad y tratamiento de datos están revisados para el alta.
- [ ] Existe manual de usuario/onboarding para el negocio.
- [ ] Existen runbooks de despliegue, backup/restore, incidentes y rollback.
- [ ] Hay responsables técnicos y comerciales, canal de soporte y escalamiento
      definidos.
- [ ] El material de venta no promete dominios personalizados, pagos automáticos ni
      métricas de ingresos.

### G. Piloto, incorporación de clientes y aprobación

- [ ] Se ejecutó un piloto controlado con 1 a 3 negocios reales después de
      preproducción.
- [ ] Cada cliente piloto firmó o aceptó términos, privacidad y tratamiento de datos.
- [ ] Se definieron criterios de éxito, capacitación y proceso de salida por cliente.
- [ ] No hubo incidentes críticos de aislamiento, pérdida de datos ni exposición de
      secretos durante el piloto.
- [ ] El feedback crítico del piloto fue resuelto o aceptado explícitamente.
- [ ] Un responsable técnico y un responsable comercial firmaron el go/no-go.
- [ ] El commit candidato, el estado de migraciones, el backup y el rollback fueron
      verificados antes del lanzamiento.
- [ ] La preproducción AWS y los smoke tests post-despliegue pasaron antes del
      lanzamiento.

---

## 11. Endurecimiento de backups S3 y operación (2026-11-24)

| Ítem | Estado | Evidencia / criterio de salida |
|---|---|---|
| Subida S3 eficiente para dumps grandes | `[PARCIAL]` | `scripts/upload_s3_backup.py` usa `upload_file()` con `TransferConfig` (multipart 16 MiB, umbral 64 MiB, 4 hilos) en lugar de `read_bytes()`/`put_object()`. conserva SHA-256 local/remoto, sidecar atómico, `pg_restore --list` y reintentos. Falta validación con un dump real de ~2 GB. |
| Backups pendientes | `[EN CURSO]` | La unidad propuesta procesa dumps `turnobot-pg-YYYYMMDD-HHMMSS.dump` en orden más reciente primero y omite los ya confirmados por marcador en `/home/ubuntu/turnobot/state/s3_backup_uploads/`. El marcador se escribe solo tras verificación remota y hash local final. Falta definir el tope por ejecución y ejecutar la unidad en AWS con autorización. |
| Servicio systemd S3 | `[PENDIENTE]` | La propuesta usa `/home/ubuntu/turnobot/.venv312/bin/python`, no `/usr/bin/python3`, y `TimeoutStartSec` ampliado. Requiere `systemd-analyze verify` e instalación en AWS; **no se aplicó nada en producción**. |
| Alertas y observabilidad | `[PENDIENTE]` | Se documentan marcadores de log (`UPLOAD_OK`, `S3_RETRY`, `S3_ERROR`, `REMOTE_VERIFICATION=OK`, `S3_UPLOAD_PENDING`, `S3_UPLOAD_SUMMARY`) y el disparo `OnFailure=turnobot-failure-notify@%N.service`. No se enviaron correos ni notificaciones reales. |
| Revisión de configuración AWS/OCI | `[PARCIAL]` | Evidencia previa read-only: AWS EC2 con `/home/ubuntu/turnobot` en `main@ddef5c0`, `turnobot.service` activo, `/health` 200, PostgreSQL 18.6 schema v6; OCI sigue siendo el servidor anterior. No hubo nueva conexión remota en esta revisión. |
| MyPy sobre archivos modificados | `[EN CURSO]` | `[tool.mypy].files` incluye los archivos Python modificados y sus dependencias corregidas; CI ejecuta `python -m mypy` sin relajar reglas. La comprobación definitiva corresponde a GitHub Actions. |

---

## Registro de avances

Actualizar este registro cada vez que se complete una etapa. No borrar filas
anteriores: agregar una nueva fila con la fecha y la evidencia concreta.

**Ámbito:** `Local` = cambio presente en un worktree, sin commit; `Sin commit` =
no integrado en git; `Integrado` = commit en GitHub; `CI` = workflow aprobado en el
commit candidato. En esta revisión **no hay filas Integrado ni CI** para el HEAD
actual. La ejecución local reportada de 20 tests en Python 3.13.14 **no** equivale a
CI aprobado.

| Fecha | Etapa | Ámbito | Estado | Evidencia / cambios | Próximo paso |
|---|---|---|---|---|---|
| 2026-11-06 | Documento | Local (`C:\Proyectos\turnobot`) | Completado local; no integrado | Creación y revisión 2 de `docs/product/PLAN_TURNOBOT_V1.md` con auditoría del repositorio y hoja de ruta. El documento **no está** en `turnobot-main-clean`. Sin commit ni push. | Trasladar e integrar en `turnobot-main-clean` con autorización explícita. |
| 2026-11-06 | E0. Línea base | Local | En curso | Revisión de `README.md`, `AGENTS.md`, `docs/`, rutas, servicios, base y git. HEAD local `e57ad6b`; `origin/main` `4bb5a1a`. Venv local Python 3.13.14; CI fija 3.12. `TURNOBOT_PG_URL` definida; suite completa no ejecutada en esta revisión. | Cerrar E1 y ejecutar E2 en Python 3.12 + PostgreSQL 16. |
| 2026-11-06 | E1. Higiene de rutas de dominios | Local / sin commit / sin CI | En curso | El commit `4bb5a1a` no registraba las rutas. En `turnobot-main-clean` hay corrección local en tres archivos: `routes/__init__.py` (registra `business_domains.register(app)`), `tests/test_standalone_create_app.py` (mapa 136 rules / 103 endpoints) y `tests/test_business_domain_route_registration.py` (nuevo; 18 rutas y 401). Según la última ejecución local: **20 tests OK** con Python 3.13.14; `python -m ruff format --check --exclude ".venv*" .` => **208 files already formatted**; `python -m ruff check --exclude ".venv*" .` => **All checks passed!**. Esa evidencia **no sustituye** la suite completa ni CI. **Falta:** PostgreSQL, Python 3.12, CI, commit/integración y matriz completa de autorización/aislamiento/CSRF; el 401 aislado no alcanza. Es higiene técnica recomendable, no activación comercial de dominios. | Validar en el entorno soportado y commitear con autorización. |
| 2026-11-06 | E2. Validación integral | Local | Pendiente | Sin ejecución completa en Python 3.12 + PostgreSQL para el HEAD actual. La corrida local reportada es parcial (20 tests) y no sustituye la suite ni CI. | Ejecutar suite, lint, formato, typecheck y migraciones en entorno aislado; obtener CI verde. |
| 2026-11-06 | E2.5. Preproducción AWS | Local | Pendiente | No se desplegó en preproducción ni se accedió a AWS en esta revisión. | Desplegar en staging, smoke tests, validar secretos/SMTP/health y probar rollback. |
| 2026-11-06 | E3. Modelo comercial | Local | Pendiente | No se encontró código de suscripción, planes, pagos ni facturación. El alta y la aprobación manual existen; el cobro manual no tiene registro en la app. El modelo es suscripción mensual con cobro manual, sin recurrencia automática ni pasarela. | Definir precio, proceso de cobro y runbook de suspensión. |
| 2026-11-06 | E4. Piloto | Local | Pendiente | Depende de E2, E2.5 y E3. | Seleccionar negocios piloto y criterios de salida. |
| 2026-11-06 | E5. Lanzamiento V1 | Local | Pendiente | Depende del cumplimiento de la sección 10 y de E2.5/E4. | Preparar checklist de go/no-go. |
| 2026-11-06 | E6. Dominios personalizados | Local | Futuro | Fuera del alcance inicial de lanzamiento. La implementación existente está parcial y sin ingress/TLS automatizado. | Reevaluar después de V1 con un proceso operativo validado. |
| 2026-11-24 | Recuperación de rama | Integrado (rama `recovery/local-improvements-20261124`) | CI verificado para la base | Commit `d9e8a607` recuperó cambios locales; commit `2e2128bb` declaró `boto3` y el marker `pg`. Push a la rama de recuperación; CI run `45396217542` en verde. Sin merge a `main`. | Validar los cambios pendientes de esta revisión antes de cualquier integración. |
| 2026-11-24 | Endurecimiento S3/operación | Local (`recovery/local-improvements-20261124`) | En curso | Reparación UTF-8 de `AGENTS.md`, `pyproject.toml`, `PLAN_TURNOBOT_V1.md` y `tests.yml`; subida multipart, marcadores de pendientes, servicio systemd con venv, alertas documentadas, tests de pools y MyPy explícito. Sin commit, push ni AWS en esta revisión. | Ejecutar validaciones locales; tras autorización, commitear y obtener CI integral en Linux. |
