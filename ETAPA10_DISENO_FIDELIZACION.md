# ETAPA 10 — Sistema de Fidelización de TurnoBot

**Tipo de documento:** Diseño técnico/producto (NO implementación)
**Alcance:** Diseño del MVP de fidelización. No modifica código, no crea migraciones, no hace commit ni push.
**Estado:** Diseño APROBADO e IMPLEMENTADO en la Etapa 10.1 (migración 009 + `services/loyalty.py` + panel `/admin/fidelizacion`; ver sección 26 "Revisión técnica pre-implementación" y sección 24 "MVP recomendado").

---

## 1. Resumen ejecutivo

TurnoBot ya administra turnos de forma multi-tenant, autenticada, auditable y probada
(>300 tests). La Etapa 10 incorpora un **sistema de puntos de fidelización** genérico y
multi-tenant con el objetivo comercial de **aumentar la frecuencia de visita y retener
clientes**, sentando las bases auditables para evolucionar hacia:

- recompensas y canjes (fase futura),
- recuperación de clientes inactivos (fase futura),
- asistencia con IA (fase futura).

El diseño apoya la regla comercial más simple y robusta:

> **Un turno COMPLETADO genera `N` puntos configurados por el negocio.**
> Reservar, cancelar o no presentarse NO genera puntos.

Para garantizar **auditoría**, **idempotencia** y **aislamiento por tenant**, el saldo
NO se guarda como un número mutable sin respaldo: se reconstruye siempre desde un
**móvil de movimientos (points_ledger)** inmutable. Durante el MVP se mantiene además
un **saldo materializado (loyalty_accounts)** para lecturas rápidas, recalculable y
reconciliable contra el ledger.

No se introducen microservicios, Redis, colas, brokers, gamificación compleja ni
frameworks de IA. Todo corre sobre la arquitectura actual (Flask + SQLite + migraciones
SQL + servicios de capa de negocio).

---

## 2. Objetivo comercial

- Incrementar la **frecuencia de visita** premiando la asistencia efectiva.
- **Retener** clientes que ya consumen, haciendo visible su progreso.
- Dar al negocio visibilidad de **clientes frecuentes** y preparar la detección de
  **clientes inactivos / en riesgo**.
- Sentar una base de datos de **historial de clientes** (por tenant) reutilizable para
  campañas, recuperación e IA en etapas futuras.

La primera versión NO busca gamificación avanzada, sino un mecanismo **simple,
predecible y auditable** que el negocio puede activar con una sola configuración.

---

## 3. Casos de uso principales

1. **Configurar fidelización** — El administrador (owner/admin) activa/desactiva
   fidelización y define cuántos puntos otorga un turno completado.
2. **Acreditar puntos** — Al marcar un turno como `completed`, el sistema intenta
   acreditar puntos; si fidelización está desactivada o el punto ya se acreditó, la
   transición de estado ocurre igual (los puntos son una consecuencia opcional).
3. **Ver saldo** — El administrador consulta el saldo acumulado de cualquier cliente
   del negocio. (El cliente lo verá en la UX pública en fase posterior.)
4. **Ver historial/movimientos** — El administrador audita cada movimiento (+/-) con
   motivo, referencia al turno, fecha y autor.
5. **Ajuste manual** — El administrador suma o resta puntos a un cliente (corrección,
   cortesía) dejando traza de quién, cuándo y por qué.
6. **Distinguir clientes frecuentes** — El panel ordena clientes por puntos/turnos
   completados dentro del negocio.
7. **Reconciliar** — El saldo materializado se puede regenerar a partir del ledger para
   detectar corrupción o errores de cómputo.
---

## 4. Reglas de negocio

### 4.1 Cuándo se generan puntos
- Transición de estado del turno a **`completed`** dentro de un negocio con
  **fidelización activada** y **points_per_completed > 0**.

### 4.2 Cuántos puntos
- Exactamente la configuración del negocio (`loyalty_settings.points_per_completed`),
  en el momento del cambio a `completed`. Se guarda el valor en el movimiento
  (**snapshot**), de modo que un cambio posterior de configuración no altere puntos ya
  acreditados.

### 4.3 Cuándo NO se generan
- Turnos `confirmed`, `cancelled`, `no_show`.
- Turnos `completed` en negocios con fidelización desactivada.
- Turnos sin cliente identificable (sin `phone` NI `customer_email`).
- Un turno que YA generó puntos (idempotencia, ver sección 7).

### 4.4 Si un turno pasa de `completed` a otro estado
- **No se restan puntos automáticamente.** Esto evita un doble registro y mantiene
  predecible la regla: "lo ya acreditado no se deshace solo". El administrador que deba
  revertir una acreditación usa el **ajuste manual negativo** (movimiento con motivo
  `reversal`), que queda auditado.
- **Regla confirmada (sticky award):** una vez que un `completed` genera puntos, el
  saldo queda como está aunque después el turno pase a `cancelled`/`no_show`. Re-marcar
  el mismo turno como `completed` NUNCA vuelve a acreditar (idempotencia). La única
  forma de deshacer es el ajuste manual auditado. Esto elimina situaciones de puntos
  "incorrectos o duplicados" por estados posteriores.
- **Decisión cerrada (ver sección 26):** en v1 NO se auto-revierte; el ajuste manual es
  la vía de corrección. Queda registrado y auditable.

### 4.5 Si un turno `completed` vuelve a procesarse (doble acreditación)
- **Prohibido por constraint.** El sistema intenta insertar el movimiento con flag
  `reference_appointment_id` bajo un `UNIQUE (business_id, appointment_id, type)`.
  Si el turno ya tiene un `earn`, el segundo intento no inserta nada y no se
  acreditan puntos (ver sección 7).

### 4.6 Cómo se corrige una acreditación incorrecta
- Mediante el **ajuste manual negativo** (`type='adjust'`, `delta<0`, `reason='reversal'`,
  opcionalmente `reference_appointment_id`). Queda en el ledger con autor y fecha.
  Nunca se edita ni borra un movimiento del ledger (inmutabilidad).
- **Regla confirmada (ver sección 26):** un ajuste NEGATIVO que dejaría el saldo por
  debajo de `0` se RECHAZA. El saldo nunca es negativo, lo que mantiene `SUM(delta)`
  coherente y evita saldos sin sentido. Si una reversión requiere absorber más del
  saldo disponible, el admin corrige el historial moviendo saldo entre cuentas del mismo
  tenant (flujo manual documentado) o admite el reclamo; no se permite inventar puntos.

### 4.7 Cliente perteneciente a otro negocio
- No existe un "cliente global": el saldo y los movimientos son conceptos **scoped por
  `business_id`**. Un turno de negocio B **nunca** acredita puntos en el negocio A
  porque la acreditación exige `appointment.business_id == business_id` y el ledger
  inserta con ese mismo `business_id`. No hay identidad global que cruzar.

### 4.8 Si el negocio desactiva fidelización
- La acreditación se detiene para turnos futuros (`completed` ya no acredita).
- **Los puntos ya acumulados se conservan** y el saldo sigue visible. No se borran ni
  expiran (v1). Esto respeta el principio de no destruir saldo acumulado sin acción
  explícita.

### 4.9 Re-cómputo / reconciliación
- `SUM(delta)` de `points_ledger` del mismo `business_id` + cuenta debe igualar el
  saldo materializado de `loyalty_accounts`. Si difieren, se regenera el saldo desde el
  ledger.

---

## 5. Modelo de datos propuesto

### 5.1 Identidad del cliente (DECISIÓN CONFIRMADA — ver sección 26)

Hoy `appointments` guarda `customer_name`, `phone`, `customer_email` SIN tabla
`customers`, y **`phone` es obligatorio al reservar** (`validate_phone` exige ≥7
dígitos), por lo que todo turno tiene un `phone` normalizado. Ese es el ancla de
identidad más fiable que ya usa TurnoBot (`get_customer_appointments` empareja por
`LOWER(customer_name)` + `phone` normalizado).

**Estrategia de identidad del MVP (cerrada):**
- **Único ancla por tenant:** `business_id + phone_normalizado`. Dos turnos del mismo
  negocio con el `phone` normalizado idéntico corresponden al MISMO cliente, aunque el
  nombre o el email cambien.
- **Sin tabla `customers` en v1** (ver sección 17). El `customers` (con `last_visit`,
  frecuencia, merge de cuentas) se pospone a la fase de recuperación/campañas; no se
  necesita para el MVP y NO se normaliza la arquitectura solo por eso.
- **`customer_email` es auxiliar**: se guarda en `loyalty_accounts` **en minúsculas y
  recortado** como dato de contacto, pero NO es una segunda clave única en v1. Evita el
  caso ambiguo de "dos personas que comparten un email": esas NUNCA deben fundirse en
  una sola cuenta (dos `phone` distintos = dos cuentas, correcto).
- **`name` es snapshot de UI** (último nombre conocido) y se actualiza en cada earn;
  NO participa de la identidad.
- **Normalización:** reutilizar `normalize_phone` de `services/appointments.py`
  (quita espacios, `()-`, `+` inicial, valida dígitos). Email: `strip().lower()`.
- **Cambio de teléfono (escenario G):** se trata como un cliente distinto (cuenta
  nueva). Es una limitación aceptada del MVP, mitigable luego con un "unir cuentas" en
  la fase `customers`. En v1 no se construye merge automático.

**Escenarios A–I resueltos (detalle en sección 26):** con el ancla único por phone se
resuelven correctamente A, B, C (por diseño se separan), D (cuentas distintas, seguro),
E (phone presente, ok), F (no aplica en v1: phone obligatorio), G (cuenta nueva),
H (sin colisión, phone distinto por persona), I (normalización de phone y email).

### 5.2 Tablas nuevas

**Migration propuesta: `009_loyalty.sql`** (el patrón actual usa una migración por feature):

```
loyalty_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,          -- fidelización on/off
    points_per_completed INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (business_id) REFERENCES businesses(id)
)

loyalty_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    customer_phone TEXT NOT NULL,                 -- ANCLA ÚNICA (phone normalizado)
    customer_email TEXT,                          -- auxiliar (strip().lower()); NO key en v1
    name TEXT,                                    -- último nombre conocido (snapshot UI)
    balance INTEGER NOT NULL DEFAULT 0,           -- saldo materializado (recalculable)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, customer_phone),          -- una cuenta por cliente y tenant
    FOREIGN KEY (business_id) REFERENCES businesses(id)
)

points_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,                       -- >0 earn/adjust; <0 redeem/adjust/reversal
    type TEXT NOT NULL,                           -- 'earn' | 'adjust' | 'redeem'(futuro)
    reason TEXT NOT NULL DEFAULT '',              -- motivo legible
    reference_appointment_id INTEGER,             -- turno que originó el movimiento
    points_per_completed INTEGER,                 -- snapshot de config (solo earn)
    actor_user_id INTEGER,                        -- autor de ajuste manual (admin); NULL si es earn automático
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (business_id) REFERENCES businesses(id),
    FOREIGN KEY (account_id) REFERENCES loyalty_accounts(id),
    FOREIGN KEY (reference_appointment_id) REFERENCES appointments(id),
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
)
```
### 5.3 Índices (constraints de integridad)

```sql
-- Un turno solo puede acreditar UNA vez dentro del negocio (idempotencia).
CREATE UNIQUE INDEX idx_points_ledger_earn_once
ON points_ledger (business_id, reference_appointment_id)
WHERE type = 'earn' AND reference_appointment_id IS NOT NULL;

-- Lecturas por cuenta y reto-reconciliación.
CREATE INDEX idx_points_ledger_account
ON points_ledger (account_id, id);
CREATE INDEX idx_points_ledger_business_created
ON points_ledger (business_id, created_at);

-- Consulta de clientes por negocio (panel, "clientes frecuentes").
CREATE INDEX idx_loyalty_accounts_business_balance
ON loyalty_accounts (business_id, balance DESC);

-- Settings por negocio ya cubierto por UNIQUE(business_id).
```

### 5.4 Por qué este diseño (vs. alternativas)

- **`loyalty_accounts` (saldo materializado) + `points_ledger` (entidad única de verdad)**
  es equivalente a un ledger contable clásico: el saldo es una proyección del libro
  mayor. Aporta auditoría total y permite la regla "saldo NO depende de un número mutable
  sin historial".
- **`loyalty_settings` separada en vez de columnas en `business_settings`**: mantiene el
  alcance cohesionado, evita tocar funciones existentes de settings y permite configurar
  sin sobrecargar `update_business_settings_scoped`.
- **Clave natural `phone`/`email` por tenant** en lugar de una tabla `customers`:
  reutiliza los datos ya capturados en `appointments`, sin migración destructiva. La
  futura tabla `customers` se puede conectar por `business_id + phone` sin migración de
  datos del ledger.

---

## 6. Flujo de puntos

```
Marcar turno como 'completed'  (admin, cambio de estado)
        |
        v
update_appointment_status_scoped(appointment_id, 'completed', business_id)
        |
        v
  (hook mínimo de fidelización, en el mismo endpoint de estado)
loyalty_service.award_points_for_completed(appointment, business_id)
        |
        v
  si fidelización deshabilitada   -> sin puntos (ok, 'disabled')
  si no hay phone ni email        -> sin puntos (ok, 'no_customer')
  si points_per_completed <= 0    -> sin puntos (ok, 'config_zero')
        |
        v
  BEGIN IMMEDIATE
   upsert loyalty_accounts (business_id + phone/email)  -> account_id
   INSERT INTO points_ledger (delta=+P, type='earn', reference_appointment_id, snapshot)
     -- la UNIQUE idx_points_ledger_earn_once aborta duplicados
   UPDATE loyalty_accounts SET balance = balance + P, name = ?
  COMMIT  (si IntegrityError -> ROLLBACK; no duplica; se loguea)
```

### 6.1 Regla de transición del estado

Para **no acoplar** la capa HTTP al ledger, la transición de estado y la acreditación
se ejecutan en `database`/`services` separadas, **invocadas por el mismo endpoint de
estado**, normalmente en la misma transacción. Si la acreditación falla por
idempotencia u off, el cambio de status **NO se revierte** (los puntos son consecuencia
opcional), pero se loguea. Esto permite "completar un turno con fidelización off" sin
romper nada.

---

## 7. Idempotencia y consistencia

Principio rector: **la acreditación solo puede existir una vez por turno y por tenant.**

1. **Constraint único parcial** `idx_points_ledger_earn_once` sobre
   `(business_id, reference_appointment_id) WHERE type='earn'` — última línea de defensa:
   aunque dos requests entren en paralelo, un solo INSERT gana; el otro recibe
   `sqlite3.IntegrityError` y se ignora.
2. **Transacción atómica** con `BEGIN IMMEDIATE` (patrón ya usado en TurnoBot): upsert de
   cuenta + insert de ledger + update de saldo en el mismo commit; ante conflicto,
   `ROLLBACK` y no hay saldo parcial.
3. **Chequeo previo OPCIONAL** (`SELECT 1 FROM points_ledger WHERE earn`) para evitar
   trabajo redundante y dar mensajes claros, pero NO se confía en él como única defensa.
4. **SQLite + WAL + busy_timeout 10s** ya configurados: dos procesos concurrentes
   serializan la escritura; el que se bloquea espera y luego cae en el constraint.
5. **Ajustes manuales** se registran como `type='adjust'`; no tocan el constraint de
   `earn`. El `reversal` referencia el movimiento a revertir (traza completa).
6. **Saldo reconstruible**: `balance = SUM(delta)` del ledger de la cuenta; la
   reconciliación compara y regenera si hay desvío (endpoint "recalcular saldo").

---

## 8. Multi-tenant y seguridad

El límite de aislamiento es **`business_id`**, igual que el resto de TurnoBot.

**Puntos donde se aplican verificaciones de autorización:**

1. **Capa de acceso a datos (`database`)** — TODA función de fidelización es
   `*_scoped(business_id, ...)` y filtra/inserta con ese `business_id` derivado de la
   capa superior (`resolve_business` por slug). Nunca se acepta un `business_id` aportado
   por el cliente para leer otro tenant.
2. **Capa de negocio (`services/loyalty.py`)** — valida que `appointment.business_id`
   coincida con el `business_id` de la operación antes de acreditar.
3. **Capa HTTP (`app.py`)** — rutas admin (config, saldo, movimientos, ajuste) usan
   `_require_admin_membership()` y `get_current_business_id()` (derivado del slug),
   replicando el patrón actual de `admin_*`.
4. **Endpoints de ajuste manual** — además de `_require_admin_membership()`, verifican
   que la cuenta objetivo pertenezca al negocio resuelto y registran al actor
   (`actor_user_id`).
5. **Ninguna consulta** de saldo/movimiento/recompensa se ejecuta sin `business_id`.
6. **Invariante cross-table (chequeo en capa de negocio + test):** al acreditar debe
   cumplirse `points_ledger.business_id == loyalty_accounts.business_id ==
   appointments.business_id` del turno. SQLite no puede imponer este invariante entre
   tablas con FKs simples, por lo que se valida en `services/loyalty.py` y se cubre con
   pruebas de aislamiento.

No se rehace la auditoría general de seguridad ya existente (CSRF, sesiones,
memberships, rate limiting); se reutiliza.
---

## 9. Experiencia del administrador (panel)

Diseño de interfaz **simple**, dentro del esquema actual de `admin.html` (sidebar +
tarjetas). Se propone un nuevo ítem de navegación **"Fidelización"** visible para
`owner`/`admin`.

**9.1 Configuración (tarjeta "Configuración de fidelización")**
- Interruptor activar/desactivar fidelización.
- Campo numérico "Puntos por turno completado" (mín 0), con valor predeterminado `1`.
- Botón "Guardar". Feedback en línea (éxito/error) como los formularios existentes.
- Texto de ayuda: "Los puntos se otorgan cuando un turno se marca como completado."

**9.2 Clientes con puntos (tarjeta/tabla "Clientes")**
- Columnas: Cliente (nombre), Teléfono/Mail, Puntos (saldo), Turnos completados.
- Ordenable por saldo descendente (índice `idx_loyalty_accounts_business_balance`).
- Cubre "consultar clientes con puntos", "saldo" y "clientes frecuentes".

**9.3 Detalle de cliente (movimientos, ajuste)**
- Al hacer clic en un cliente: historial de movimientos (fecha, tipo, puntos, motivo,
  turno asociado, autor).
- Botón "Ajustar puntos" -> formulario de ajuste (suma/resta), motivo obligatorio.
  Registra `actor_user_id`, `reason`, `created_at`.

**9.4 Reconciliación**
- Botón discreto "Recalcular saldo" (admin-only) que regenera balances desde el ledger
  y reporta si había desvíos. Acción de mantenimiento, no parte del flujo diario.

**No se convierte el panel en un CRM.** No hay búsqueda avanzada, segmentación ni
export en v1.

---

## 10. Experiencia del cliente (pública)

**Alcance v1: mínima pero preparada para evolucionar.**

- Mientras el cliente gestiona su turno (`gestionar_turno.html`,
  `appointments/customer_appointments`), se muestra un **bloque informativo de
  "Mis puntos"** que consulta `get_balance_scoped(business_id, phone/email)` del
  negocio: saldo actual y (opcional) últimas acreditaciones.
- Si fidelización está desactivada o el negocio aún no la adopta, **no se muestra nada**
  (no inventar puntos para un sistema apagado).
- **NO** hay recompensas, canje ni carrito en v1. El bloque es un simple
  "Σ puntos acumulados — sumá 1 punto por visita completada" con orientación a
  "pronto: beneficios".
- La UI es deliberadamente reutilizable: el mismo acceso `get_customer_points` servirá
  más adelante para listar recompensas, progreso y estados "canjeable/no canjeable".

---

## 11. Configuración del negocio

| Configuración | Tipo | Default | Observaciones |
|---|---|---|---|
| `enabled` | bool | `0` (off) | Fidelización arranca **apagada** para no cambiar el comportamiento por defecto; el negocio la activa explícitamente. |
| `points_per_completed` | int >= 0 | `1` | Se guarda valor snapshot por movimiento: cambiar la config NO reabre turnos pasados. |

**Por qué default** `enabled=0`: el MVP no debe alterar la operación diaria de negocios
existentes. `points_per_completed=1` es una unidad neutra y fácil de entender.

**Solo dos configuraciones** para evitar sobreingeniería. No se agregan aún vencimientos,
multiplicadores por servicio ni bonuses.

---

## 12. Estrategia de tests (antes de implementar)

Suite nueva `tests/test_loyalty_*.py`, reutilizando el patrón de temp-DB + dos negocios +
login admin. Casos mínimos:

**12.1 Reglas de puntos**
- `completed` acredita saldo correcto (config=1).
- `completed` con config>1 acredita el valor configurado.
- `cancelled`, `no_show`, `confirmed` NO acreditan (saldo intacto, sin movimientos).
- Turno completado con fidelización OFF no acredita.

**12.2 Idempotencia**
- Llamar 2 veces a `award_points_for_completed` para el mismo turno: 1 solo movimiento.
- Reintentar tras error de integridad no duplica.
- (Opcional) invocación concurrente: solo una INSERT gana (constraint).

**12.3 Ajustes**
- Ajuste manual positivo/negativo registra movimiento, actualiza saldo, guarda motivo y actor.
- Reversal de una acreditación deja saldo correcto y traza.

**12.4 Aislamiento multi-tenant**
- Cliente "mismo teléfono" en negocio A y B tienen saldos independientes.
- Turno de B completado desde el negocio B no acredita en A y viceversa.
- El panel de A no puede ver/ajustar saldo de B (HTTP 403/redirect y chequeo a nivel service).

**12.5 Configuración**
- Guardar/leer `enabled` y `points_per_completed` por negocio, aislados.
- Desactivar fidelización detiene acreditaciones futuras pero conserva saldo.

**12.6 Consistencia del ledger**
- `SUM(delta)` de toda la cuenta == saldo materializado.
- Recalcular saldo regenera el desvío correcto si se inyecta una anomalía.
---

## 13. Cambios previstos en cada módulo

| Módulo | Cambio |
|---|---|
| `migrations/009_loyalty.sql` | Nueva migración con `loyalty_settings`, `loyalty_accounts`, `points_ledger` + índices. (NO crear hasta aprobar diseño.) |
| `database/database.py` | Helpers scoped: CRUD `loyalty_settings`, upsert `loyalty_accounts`, insert `points_ledger` idempotente, `get_balance`, `list_ledger`, `list_customers_by_balance`, ajuste manual, recálculo. |
| `services/loyalty.py` (nuevo) | Capa de negocio: `award_points_for_completed`, `adjust`, `get_balance`, `list_movements`, reglas de la sección 4. Devuelve dicts `{"success","reason",...}`. |
| `services/appointments.py` | Modificación **mínima**: al transicionar a `completed`, invocar `loyalty.award_points_for_completed` (o exponer un hook llamado por `app.py`). |
| `app.py` | Endpoints admin `/admin/fidelizacion` (config, lista, detalle, ajuste, recalcular) con doble ruta `/b/<slug>/...` + `_require_admin_membership()`. Endpoint público `/api/puntos` para la UX del cliente. |
| `templates/admin.html` | Ítem de navegación "Fidelización" + secciones de configuración, clientes y detalle/ajuste. |
| `templates/gestionar_turno.html` | Bloque informativo "Mis puntos" (condicional a fidelización activa). |
| `static/admin.css` / `static/app.js` | Estilos del bloque/módulo y JS mínimo para ajuste/recalcular. |
| `tests/test_loyalty_*.py` | Nueva suite (sección 12). |

**Dependencias:**
- Depende de `appointments.status` (`completed`) ya existente, `businesses`, `users`,
  membresías `owner/admin` y del patrón `notification_log` (modelo de idempotencia).
- No modifica el esquema de `appointments` salvo que se decida en 4.4/21 un flag
  "points_awarded" explícito; de optarlo, sería una columna adicional con default y el
  constraint del ledger seguiría siendo la defensa real.

---

## 14. Impacto en la arquitectura (resumen de entregables)

- **Tablas nuevas:** 3 (`loyalty_settings`, `loyalty_accounts`, `points_ledger`).
- **Índices:** 4 (un UNIQUE parcial de idempotencia + 2 de consulta + 1 de orden).
- **Migración:** 1 (`009_loyalty.sql`) — solo tras aprobar el diseño.
- **Servicios:** 1 nuevo (`services/loyalty.py`); helpers en `database.py`.
- **Endpoints:** admin (config, lista, detalle, ajuste, recalcular) + 1 público
  (`/api/puntos`).
- **Frontend:** `admin.html`, `gestionar_turno.html`, CSS/JS menores.
- **Config:** 2 campos (`enabled`, `points_per_completed`) en `loyalty_settings`.
- **Tests:** +1 suite nueva (sección 12).
- **Appointments:** modificación mínima solo en la transición `-> completed`.

---

## 15. Evitar sobreingeniería (declaración explícita)

**NO** se introducen: microservicios, Redis, colas, event brokers, blockchain,
gamificación compleja, sistemas externos, LangGraph/agentes, ni frameworks nuevos.
Se reutiliza el stack actual (Flask + SQLite + migraciones SQL + capas de negocio).

---

## 16. Evolución futura hacia recompensas (NO implementar)

El modelo ya prepara este salto sin breaking changes:

- **Recompensas (catálogo):** tablas futuras `loyalty_rewards`
  (business_id, name, cost_points, active, benefits) — nuevas, sin tocar el ledger.
- **Canje:** usar el mismo `points_ledger` con `type='redeem'`, `delta<0`, y una
  `loyalty_redemptions` que referencie reward + ledger entry + fecha. La idempotencia del
  canje usa el mismo patrón de UNIQUE por tenant.
- **Vencimiento:** leer en la UI solo movimientos `delta>0` no consumidos más antiguos;
  el saldo sigue reconstruible. Se puede añadir `expires_at` a movimientos positivos más
  adelante sin cambiar la arquitectura de saldo.
- **Beneficios / progreso:** derivables de `SUM(delta)` por tipo y del historial, sin
  nuevas tablas obligatorias.
- La regla "saldo = SUM(delta)" es **neutral** respecto a canjes y vencimientos, por lo
  que la evolución no rompe la auditoría.
---

## 17. Evolución futura hacia recuperación de clientes (NO implementar)

Para detectar oportunidad de recuperación, TurnoBot debe poder responder por tenant:
- **frecuencia habitual** -> `COUNT` de appointments `completed` agrupados por
  `business_id + phone` en una ventana;
- **última visita** -> `MAX(appointment_date)` + estado `completed`;
- **clientes inactivos / dejaron de reservar** -> ventanas de tiempo desde última visita;
- **clientes frecuentes** -> ranking de `completed`/puntos (ya cubierto en puntos).

**Qué conservar hoy para no bloquear esto:**
- Historial completo e inmutable de `appointments` (incluye completados pasados).
- Ledger con `business_id + customer + created_at` (historial de actividad por tenant).
- Capturar `customer_email` y `phone` normalizados en el ledger y, si se decide v1 sin
  tabla `customers`, mantener al menos `name` snapshot por cuenta para ranking legible.
- La futura tabla `customers (business_id, phone, email, last_visit, visit_count)` se
  puede materializar desde `appointments` y el ledger sin migración destructiva.

---

## 18. Evolución futura hacia IA (NO implementar)

Principio: **la IA debe quedar desacoplada de la lógica determinista de puntos.**

- La capa de puntos es 100% determinista y auditable (NO la toca la IA).
- La IA (etapa futura) se alimentará de **vistas/derivaciones** ya existentes por
  tenant: historial de fidelización (`points_ledger`), frecuencia (`appointments`),
  inactividad, ranking. TurnoBot podría proponer:
  - detectar clientes con probabilidad de volver (frecuencia pasada - días sin turno);
  - sugerir campañas / descuentos / recordatorios personalizados;
  - generar mensajes personalizados al estilo del asistente actual
    (`services/ai.py` ya expone `ask_ai` con `business_id`), reutilizando ese canal de
    LLM sin introducir agentes.
- Si se usara IA, sería como **asistencia al admin** (sugerencias), NUNCA como ejecutor
  de la contabilidad de puntos. El ledger es la fuente de verdad; la IA solo lee
  agregaciones.

---

## 19. Riesgos

| Riesgo | Mitigación |
|---|---|
| Doble acreditación | UNIQUE parcial por tenant + transacción `BEGIN IMMEDIATE` + chequeo previo. |
| Saldo divergente del historial | Ledger inmutable; saldo materializado reconciliable/recalculable. |
| Aislamiento roto entre tenants | Pantalla total `*_scoped(business_id)` derivado del slug; tests de aislamiento. |
| Sobreingeniería | Alcance v1 mínimo (2 configs, 3 tablas, 1 flujo). |
| Cambio de regla de negocio en caliente | Snapshot de `points_per_completed` por movimiento; config no reabre pasado. |
| Identidad de cliente débil (sin tabla customers) | Clave natural `phone`/`email` por tenant; camino a tabla `customers` futuro sin migración destructiva. |

---

## 20. Decisiones tomadas

1. Regla: solo `completed` acredita; `confirmed/cancelled/no_show` no.
2. Saldo = proyección de un ledger inmutable (`points_ledger`); no un número sin respaldo.
3. Configuración por negocio en tabla dedicada `loyalty_settings` con `enabled=OFF` y
   `points_per_completed=1` por defecto.
4. Cambiar de `completed` a otro estado NO auto-compensa en v1; se exige ajuste manual
   auditado (reversal).
5. Doble acreditación bloqueada por UNIQUE parcial por tenant.
6. Identidad de cliente = **único ancla `business_id + phone_normalizado`** por tenant;
   `email` auxiliar (no key) y `name` snapshot de UI; sin tabla `customers` en v1.
7. Desactivar fidelización conserva saldo acumulado.
8. Diseño preparado para recompensas/vencimientos/canjes y para informar a IA sin que la
   IA toque la contabilidad.
---

## 21. Decisiones que todavía requieren definición

> Actualización tras la revisión técnica (sección 26): los puntos 1, 2 y 5 de esta
> lista quedaron **cerrados** (ver sección 26). Lo que resta de la lista:

1. ~~Auto-reversión vs. ajuste manual~~ → **CERRADO en v1**: NO auto-reversión; ajuste
   manual auditado (secciones 4.4, 26).
2. ~~Tabla `customers` por tenant en v1~~ → **CERRADO en v1**: NO se crea; identidad por
   `business_id + phone_normalizado` (sección 5.1, 26).
3. **Columna `appointments.points_awarded`** (flag) como redundancia de ledger o apoyo
   de UI; el ledger es la fuente de verdad. Se puede omitir en v1.
4. **Vencimiento de puntos** (definido como futuro; si el negocio lo pide pronto, se
   agrega `expires_at` a movimientos positivos).
5. ~~¿Completar un `completed` con fidelización desactivada sin puntos?~~ → **CERRADO**:
   sí, completar funciona y no acredita (los puntos son consecuencia opcional).
6. **Canjes/recompensas**: rango de esta etapa (por defecto fuera de alcance, sección 22).

---

## 22. FUERA DEL ALCANCE

Deliberadamente **NO** se implementa en esta etapa:

- Recompensas / canje / costo en puntos / validez de beneficios.
- Vencimiento de puntos.
- Gamificación (rangos, badges, streaks, niveles).
- Tabla global de clientes / CRM; búsqueda avanzada; segmentación; exportaciones.
- Auto-reversión automática de puntos al cambiar de estado de forma no explícita.
- Sistemas externos, microservicios, colas, Redis, brokers.
- IA / LangGraph / agentes (solo lectura de agregaciones en fases futuras).
- Campañas, descuentos o envíos masivos automáticos.
- Multiplicadores por servicio, promociones o bonos por fidelización en el mismo negocio.

---

## 23. Orden recomendado de implementación

**Fase 0 — Diseño y aprobación** (este documento).
**Fase 1 — Migración** `009_loyalty.sql` + helpers `database.py` (scoped).
**Fase 2 — Servicio** `services/loyalty.py` (acreditación, saldo, ajuste, recálculo).
**Fase 3 — Hook en transición a `completed`** (`appointments` / `app.py`).
**Fase 4 — Endpoints admin** (config, lista, detalle, ajuste, recalcular) con doble ruta
 y `_require_admin_membership()`.
**Fase 5 — Panel** (`admin.html` + CSS/JS): configuración, clientes, detalle/ajuste.
**Fase 6 — UX pública mínima** (`/api/puntos` + bloque en `gestionar_turno.html`).
**Fase 7 — Tests** `test_loyalty_*.py` (reglas, idempotencia, aislamiento, config, ledger).
**Fase 8 — Verificación** suite completa (>300 + nuevos), backup/restauración inmutable.

---

## 24. ETAPA 10 — MVP RECOMENDADO

**Alcance mínimo exacto a implementar primero (v1, sin las fases futuras):**

1. **Migración única** `009_loyalty.sql`:
   `loyalty_settings`, `loyalty_accounts`, `points_ledger` + índices
   (incluye UNIQUE parcial de `earn` por tenant = idempotencia).
2. **database.py**: todos los helpers `*_scoped` para config, upsert de cuenta, insert
   idempotente de `earn`, saldo, listar movimientos, ajuste manual, recálculo.
3. **services/loyalty.py** (`success/reason`): `award_points_for_completed` (reglas 4.1-
   4.9), `adjust`, `get_balance`, `list_movements`, `rebalance`.
4. **Hook mínimo**: al marcar `completed`, invocar `award_points_for_completed` sin
   romper el cambio de estado si fidelización está off / sin duplicar.
5. **Admin (mínimo)**:
   - activar/desactivar + `points_per_completed` (defaults 0/1);
   - listar clientes con saldo (ordenado desc);
   - ver movimientos de un cliente;
   - ajuste manual (suma/resta) con motivo y actor.
6. **UX pública mínima**: un endpoint `/api/puntos` + bloque estático "Mis puntos"
   cuando fidelización activa en `gestionar_turno.html`.
7. **Tests**: suite nueva que cubra 12.1-12.6, ejecutada antes del merge.

**Criterios de aceptación del MVP:**
- Un turno `completed` acredita exactamente la config del negocio, una vez.
- Cancelar / no_show / confirmed no acreditan.
- Saldos distintos entre negocios (aislamiento).
- Ajuste manual auditable con actor/motivo.
- Desactivar fidelización detiene acreditaciones futuras sin perder saldo.
- `SUM(delta)` == saldo materializado (reconciliable).
- Suite completa verde (>300 existentes + nuevos).

---

## 25. FUERA DE ALCANCE (resumen operativo)

Todo lo que NO se toca en ninguna fase de esta etapa: recompensas, canje, vencimiento,
gamificación, CRM, sistemas externos, microservicios, colas, Redis, brokers,
LangGraph/IA, campañas masivas, envío automático, multiplicadores/bonus. Ningún cambio
de arquitectura persistente; solo se agrega el modelo de fidelización sobre la
arquitectura actual de TurnoBot.
---

## 26. REVISIÓN TÉCNICA PRE-IMPLEMENTACIÓN (veredicto)

> Resultado de la revisión del diseño antes de autorizar la implementación.

### A. APROBADO SIN CAMBIOS (decisiones que se mantienen)

1. **Saldo reconstruible desde ledger** (`points_ledger`) + saldo materializado
   (`loyalty_accounts`) reconciliable. Correcto: saldo NO es un número mutable sin
   historial.
2. **Idempotencia por UNIQUE parcial en SQLite.** SQLite soporta índices parciales
   (`CREATE UNIQUE INDEX ... WHERE type='earn'`). Combinado con `BEGIN IMMEDIATE`
   (patrón ya usado en TurnoBot) y WAL+busy_timeout es una solución concreta y segura.
3. **Regla de negocio**: solo `completed` acredita; `confirmed/cancelled/no_show` no.
4. **Configuración por negocio en `loyalty_settings`** con `enabled=OFF` (default) y
   `points_per_completed=1`.
5. **Desactivar fidelización**: conservar saldo, detener nuevas acreditaciones, sin
   borrar ni congelar. Comportamiento más simple y seguro.
6. **Scope por `business_id`** en toda la capa de datos (`*_scoped`) + derivación del
   tenant por slug en HTTP. Alineado con el resto de TurnoBot.
7. **Ajuste manual con `actor_user_id`, `reason`, fecha.** Correcto y auditable.
8. **No rehacer la auditoría de seguridad** (CSRF, sesiones, memberships, rate limit).

### B. CAMBIOS RECOMENDADOS (necesarios antes de implementar)

| # | Cambio | Justificación |
|---|---|---|
| 1 | **Un único ancla de identidad** `business_id + phone_normalizado` en `loyalty_accounts` (UNIQUE). Quitar la doble key phone+email. | Elimina ambigüedad de "dos personas que comparten email" y brinda identidad determinista. |
| 2 | `customer_email` guardado **`strip().lower()`**; `name` snapshot de UI y no key; `customer_phone NOT NULL`. | Normalización exigida por escenario I; phone siempre presente hoy. |
| 3 | **Ajuste negativo que deje saldo < 0 → RECHAZADO.** | Mantiene `SUM(delta)` coherente y saldos sin sentido fuera del sistema. |
| 4 | **Regla "sticky award"** explícita (ver 4.4): no auto-reversión; re-completar nunca duplica. | Elimina puntos duplicados/incorrectos por estados posteriores. |
| 5 | **Chequeo cross-table** `points_ledger.business_id == account.business_id == appointment.business_id` en capa de negocio + test. | SQLite no puede imponer invariante entre tablas con FKs simples. |
| 6 | `actor_user_id=NULL` para earns automáticos (solo ajustes manuales registran actor). | Trazabilidad clara de origen sin sobreespecificación. |

### C. DECISIONES CRÍTICAS (estrategia de identidad de clientes)

- **Estrategia del MVP:** identidad por **`business_id + phone_normalizado`**. El phone es
  obligatorio al reservar (`validate_phone` ≥7 dígitos) y ya normalizado por TurnoBot, y
  es el mismo criterio implícito que usa `get_customer_appointments`.
- **Resolución por escenario:**
  - **A** (mismo nombre+phone+email): misma cuenta (phone coincide). ✔
  - **B** (mismo phone, nombre distinto): **misma cuenta**; se actualiza `name`. ✔
  - **C** (mismo email, phone distinto): **cuentas distintas** (no se funden). Seguro. ✔
  - **D** (mismo nombre, phone distinto): **cuentas distintas** (pueden ser personas
    distintas o TV/cambio de línea). ✔ (por defecto seguro).
  - **E** (sin email): no afecta; phone es el ancla. ✔
  - **F** (sin phone): **no aplica en v1** porque `create_appointment` exige phone. Si en
    el futuro se acepta turno sin phone, se necesita tabla `customers` (fuera de alcance).
  - **G** (cambio de phone/email): cambio de phone = cuenta nueva (limitación aceptada,
    mitigable después con "unir cuentas" en fase `customers`). Email no participa.
  - **H** (dos personas con el mismo nombre): sin colisión, cada una con su phone. ✔
  - **I** (formato): phone se normaliza con `normalize_phone`; email `strip().lower()`. ✔
- **Por qué NO se crea `customers` en v1:** el MVP no usa atributos por cliente que
  requieran tabla propia; todo se deriva de `appointments` + `points_ledger` por tenant.
  Crear `customers` ahora era sobre-ingeniería y no aporta al MVP; se pospone a la fase
  de recuperación/campañas donde sí se necesitan `last_visit`, frecuencia y merge.
### D. MODELO FINAL PROPUESTO

```
businesses (existente: id, name, slug)                 -- tenant

loyalty_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    points_per_completed INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (business_id) REFERENCES businesses(id)
)

loyalty_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    customer_phone TEXT NOT NULL,          -- ANCLA ÚNICA (normalizado con normalize_phone)
    customer_email TEXT,                   -- auxiliar strip().lower() (NO key en v1)
    name TEXT,                             -- snapshot de UI (no key)
    balance INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, customer_phone),
    FOREIGN KEY (business_id) REFERENCES businesses(id)
)

points_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,                -- >0 earn/adjust; <0 adjust/reversal
    type TEXT NOT NULL,                    -- 'earn' | 'adjust' | 'redeem'(futuro)
    reason TEXT NOT NULL DEFAULT '',
    reference_appointment_id INTEGER,      -- turno del earn (o reversal)
    points_per_completed INTEGER,          -- snapshot (solo earn)
    actor_user_id INTEGER,                 -- admin en ajustes; NULL en earns automáticos
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (business_id) REFERENCES businesses(id),
    FOREIGN KEY (account_id) REFERENCES loyalty_accounts(id),
    FOREIGN KEY (reference_appointment_id) REFERENCES appointments(id),
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
)

-- Idempotencia: un earn por appointment y tenant.
CREATE UNIQUE INDEX idx_points_ledger_earn_once
ON points_ledger (business_id, reference_appointment_id)
WHERE type = 'earn' AND reference_appointment_id IS NOT NULL;

CREATE INDEX idx_points_ledger_account ON points_ledger (account_id, id);
CREATE INDEX idx_points_ledger_business_created ON points_ledger (business_id, created_at);
CREATE INDEX idx_loyalty_accounts_business_balance
ON loyalty_accounts (business_id, balance DESC);
```

Relaciones: `loyalty_settings 1:1 business`; `loyalty_accounts N:1 business`;
`points_ledger N:1 loyalty_accounts`, `N:1 business`, `N:1 appointments` (referencial),
`N:1 users` (actor, opcional). Todo scoped y recuperable por `business_id`.

### E. REGLAS DE NEGOCIO FINALES (precisas)

1. Solo `completed` acredita; `confirmed/cancelled/no_show` NO.
2. Se acredita el valor de `points_per_completed` del negocio **al momento** del
   `completed` (snapshot guardado por movimiento).
3. Completar sin fidelización off, sin phone, o con config 0: **no acredita** (state change
   de todas formas procede).
4. Acreditación **sticky**: re-completar el mismo turno NUNCA duplica; pasar a
   `cancelled/no_show` tras acreditar NO revierte (solo ajuste manual).
5. Una cuenta por `business_id + phone_normalizado`.
6. Ajuste manual positivo/negativo registra motivo + actor + fecha; **saldo nunca < 0**
   (ajuste negativo que violaría se rechaza).
7. Desactivar fidelización conserva saldo y detiene nuevas acreditaciones.
8. Sin tabla `customers`; identificación por phone normalizado del turno.
9. Aislamiento total por `business_id` (lectura y escritura) derivado del slug.
10. Saldo reconciliable: `SUM(delta)` del ledger == `loyalty_accounts.balance`.

### F. PLAN DE IMPLEMENTACIÓN (orden exacto)

1. **Migración** `009_loyalty.sql` con las 3 tablas + índices anteriores.
2. **Helpers `database.py`** (scoped): config CRUD, upsert account por phone, insert earn
   idempotente, get_balance, list_ledger, list_customers_by_balance, adjust, rebalance.
3. **`services/loyalty.py`**: `award_points_for_completed`, `adjust`, `get_balance`,
   `list_movements`, `rebalance` (con invariante cross-table y regla de saldo >= 0).
4. **Integración appointments**: en la transición a `completed` (`app.py`/`appointments`)
   invocar `award_points_for_completed`; la falla por idempotencia/off no revierte estado.
5. **Endpoints** admin `/admin/fidelizacion` (+ `/b/<slug>/...`) con
   `_require_admin_membership()`: config, lista, detalle, ajuste, recalcular; y público
   `/api/puntos`.
6. **Frontend**: `admin.html` (nav + secciones), `gestionar_turno.html` (bloque "Mis
   puntos"), CSS/JS menores.
7. **Tests** `test_loyalty_*.py`: reglas, idempotencia, aislamiento, configuración,
   ledger, normalización, cliente sin email, sin phone (F), ajustes, cambio de estado.
8. **Validación**: suite completa (>300 + nuevos), backup/restauración, revisión en
   staging.

### G. RIESGOS (solo reales)

- **Doble acreditación por reintentos/concurrencia** → mitigado por UNIQUE parcial +
  `BEGIN IMMEDIATE` + WAL. Riesgo bajo.
- **Fusión accidental de clientes** → mitigado con ancla única por phone (email nunca
  funde). Riesgo mínimo.
- **Cambio de phone genera cuenta nueva** → limitación aceptada; sin merge en v1
  (mitigable después). Riesgo asumido y documentado.
- **Saldo divergente del historial** → reconciliación/recalcular (suma) siempre disponible.
- **SQLite no valida invariante cross-table** → chequeo en capa de negocio + tests.
- **Ajuste negativo mal usado** → regla saldo >= 0 + motivo/actor obligatorios.

### H. VEREDICTO

> **NECESITA AJUSTES DE DISEÑO** (menores, acotados y cerrados en este documento).
> No requiere rediseño: la arquitectura actual soporta el MVP sin cambios estructurales
> (no se crea `customers`, no hay `appointments.points_awarded` obligatorio, no hay
> migraciones sobre tablas existentes). Con los ajustes B/4 y la estrategia de identidad
> C incorporadas (ya aplicadas en secciones 4.4, 4.6, 5.1, 5.2, 8, 20, 21), queda
> **listo para implementar** en el orden F.