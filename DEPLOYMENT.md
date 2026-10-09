# Despliegue

## Configuración

El archivo `.env` debe contener `FLASK_ENV=production`, `COOKIE_SECURE=1`,
`SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `PUBLIC_BASE_URL` (origen HTTPS canónico),
`GEMINI_API_KEY` y, en OCI, un `DATABASE_URL` válido de PostgreSQL. Nunca debe
publicarse.

OCI producción requiere una `DATABASE_URL` válida de PostgreSQL. Si falta, está
vacía, malformada o usa un esquema distinto de PostgreSQL, el arranque falla
con un error explícito; nunca hay fallback silencioso a SQLite. En desarrollo
y tests, SQLite solo se habilita mediante `DB_BACKEND=sqlite` explícito.

## Migraciones PostgreSQL

El arranque solo aplica `migrations_pg/001_initial_schema.sql` a una base vacía.
Si ya existe `businesses`, asume que el esquema está actualizado y no aplica
migraciones incrementales. Los cambios de esquema tienen un proceso EXPLÍCITO,
separado del arranque:

```powershell
python scripts\migrate_pg.py --url "$env:DATABASE_URL" --dry-run   # revisar
python scripts\migrate_pg.py --url "$env:DATABASE_URL"             # aplicar
```

Flujo recomendado: backup → dry-run → aplicar → verificar → arrancar.

El runner (`database/pg_migrator.py`) mantiene `migrations_pg/` versionada y
registra en `schema_migrations` (`version`, `name`, `checksum`, `applied_at`):

- Aplica migraciones pendientes (`migrations_pg/00N_nombre.sql`) en orden
  determinista, cada una en su propia transacción (rollback si una falla).
- Nunca repite una ya aplicada; falla ante checksum modificado, huecos de
  versión o un registro más nuevo que el código.
- En una base vacía aplica `001_initial_schema.sql` y lo registra.
- Si la base ya fue bootstrapeada por el arranque (001 completo, sin
  `schema_migrations`), registra el baseline sin re-ejecutarlo y continúa.
- Un esquema parcial produce un error explícito sin tocar DDL.
- Usa un advisory lock compartido con el bootstrap para serializar ejecuciones
  concurrentes, con una espera **acotada**: por defecto 60 s, configurable con
  `lock_timeout` en `run_migrations`/`apply_pending_migrations`. Si otro runner
  o el bootstrap retiene el lock más tiempo, la ejecución falla con
  `MigrationLockTimeout` en lugar de quedarse bloqueada indefinidamente.

### Restricción del parser SQL

`split_sql_statements` (el parser que parte el `.sql` en sentencias) NO soporta:

- Cuerpos procedimentales `$...$` (funciones/`DO`/configuración con `$$...$$`):
  el parser parte por `;` y un `$$` no cerrado produce SQL inválido o
  sentencias truncadas.
- `SET TRANSACTION` ni `SAVEPOINT` (y el runner descarta `BEGIN`/`COMMIT`/
  `ROLLBACK` propios del archivo para conservar la atomicidad por migración).

Las migraciones deben ser **DDL puro** (una o más sentencias `CREATE`/`ALTER`/
`DROP`/`COMMENT`/`GRANT`, terminadas en `;`). Si se necesitara un cuerpo
procedimental, hay que migrar el esquema por otro medio y documentar el cambio
aquí; el parser no se va a ampliar para cubrirlo.

Cualquier cambio de esquema se agrega como un nuevo archivo
`migrations_pg/00N_nombre.sql` (N = siguiente versión) y se aplica con este
runner; `001_initial_schema.sql` no debe editarse retroactivamente.

## Arranque

```powershell
venv\Scripts\pip.exe install -r requirements.txt
venv\Scripts\python.exe wsgi.py
```

En Windows, `scripts\run_production.ps1` ejecuta el mismo proceso desde la raíz.
Para reinicio automático, usar el Programador de tareas o NSSM con ese script.

## HTTPS y proxy

Waitress debe quedar escuchando solo en `127.0.0.1`. Exponer al público mediante
IIS, Caddy o Nginx con certificado TLS. El proxy debe reenviar tráfico a
`http://127.0.0.1:5000` y permitir únicamente HTTPS desde Internet.

### Cookie de sesión y HTTPS

La cookie de sesión se marca como `Secure` únicamente si el `.env` define
`COOKIE_SECURE=1` (requerido en producción). NO debe desactivarse para "hacer
funcionar" la cookie en HTTP local de pruebas: eso debilita la protección y
puede exponer la sesión en tránsito. Si la cookie no llega en un entorno, el
problema se resuelve sirviendo la app por HTTPS detrás del proxy, no
desactivando el flag. La aplicación nunca desactiva esta protección por sí sola.

### IP del cliente y rate limiting

La aplicación no confía en `X-Forwarded-For` ni `X-Real-IP` por defecto.
Si existe exactamente un reverse proxy confiable delante de Waitress, configurar
`TRUSTED_PROXY_COUNT=1`; para una cadena de dos proxies confiables, usar `2`.
El valor debe coincidir con la cantidad real de proxies controlados por el
operador. No activarlo si la aplicación recibe tráfico directo de Internet:
un cliente podría falsificar su IP mediante cabeceras reenviadas. ProxyFix solo
se activa cuando `TRUSTED_PROXY_COUNT` es mayor que cero.

Los límites actuales son 10 intentos de login por IP, 20 mensajes de chat por
IP/negocio y 60 solicitudes por IP/endpoint/negocio para la API pública.
El tenant se obtiene del slug resuelto por la aplicación, nunca de datos del cliente.

El rate limiting usa memoria del proceso: se pierde al reiniciar y distintos
workers pueden tener buckets independientes. Para escalar horizontalmente se
necesitará posteriormente un almacenamiento compartido, como Redis.
El arranque documentado con `wsgi.py` usa un único proceso Waitress (sus threads
comparten el estado). Con más de un proceso, los límites no son globales; además,
el tope de claves puede expulsar buckets activos bajo tráfico de muchas IPs.

## Gestión pública de turnos

Al crear un turno, la API devuelve un `management_token` aleatorio. Debe
conservarse y enviarse en el cuerpo POST para cancelar o reprogramar junto con
`appointment_id`. Solo se almacena su hash SHA-256.

Para cancelar o reprogramar, el `management_token` es OBLIGATORIO y el único
factor de gestión: sin él, el turno no se modifica (el par nombre+teléfono ya
no autoriza). Se compara el hash SHA-256 del token contra
`management_token_hash` del turno, scoped por negocio y solo si el turno está
confirmado. El `appointment_id` por sí solo — incluso enumerando IDs — no
autoriza ninguna operación sobre turnos ajenos: un tercero que conozca solo el
ID no puede cancelar ni reprogramar el turno de otra persona.

`/api/turnos`, la API pública y el asistente de IA exponen únicamente columnas
públicas del turno; `management_token_hash` nunca sale en las respuestas.

## CSRF

Los formularios de login, logout y todas las operaciones administrativas POST
requieren el campo oculto `csrf_token`, asociado a la sesión Flask. Las APIs
JSON públicas (`/chat`, reservas, cancelación y reprogramación) no requieren
CSRF porque no usan la sesión administrativa como autenticación; cancelación y
reprogramación exigen `management_token` (obligatorio). El
`management_token` es independiente del token CSRF y no debe confundirse con él.

## Provisioning

El alta de negocios se administra desde el panel de superadmin, protegido por
credenciales separadas. El superadmin crea un negocio con slug propio y genera
una invitación por token; el owner pendiente la acepta (`active=0`, sin acceso
hasta completar el alta). Las invitaciones expiran según
`INVITATION_LIFETIME_HOURS` (72 h por defecto) y se pueden reenviar desde el
panel (la migración `012_platform.sql` agregó las tablas de la plataforma).
La identidad de plataforma es independiente de la de negocio. El bootstrap del
primer superadmin se hace con `python scripts/create_superadmin.py`. No se
expone ninguna creación de negocio como endpoint público.

## Backups

### OCI producción (PostgreSQL)

```powershell
venv\Scripts\python.exe scripts\backup_postgresql.py
venv\Scripts\python.exe scripts\verify_backup_postgresql.py
```

La verificación anterior comprueba que `pg_restore --list` puede leer el dump;
no equivale a una restauración completa. La prueba de restore debe realizarse
en una base desechable. No apuntar el restore de prueba a la base productiva.

`restore_postgresql.py` rechaza, **antes de ejecutar `dropdb`**, cualquier
destino que coincida con la base productiva definida por `DATABASE_URL` (host,
puerto y nombre). Si omite `DBNAME` usa un nombre temporal
`turnobot_restore_<timestamp>` (siempre seguro). Un destino que coincida con
producción aborta con error sin tocarla. El mismo nombre en otro servidor no
se considera peligroso.

En OCI, los units `turnobot-backup*` programan el backup y su verificación.
El timer `turnobot-backup-prune.timer` ejecuta el pruner SQLite sobre
`database/backups`; no retiene ni elimina dumps de `backups_pg/`.

### SQLite legacy

Los scripts siguientes se conservan para el fallback SQLite y copias históricas;
no son el mecanismo de backup de PostgreSQL:

```powershell
venv\Scripts\python.exe scripts\backup_database.py
venv\Scripts\python.exe scripts\verify_backup.py
venv\Scripts\python.exe scripts\restore_database.py database\backups\appointments-YYYYMMDD-HHMMSS.db
venv\Scripts\python.exe scripts\prune_backups.py
```

Restaurar una copia SQLite anterior al cutover no recupera ni conserva las
escrituras que se hayan realizado posteriormente en PostgreSQL; no debe
considerarse un rollback válido de producción.

## Health check y pruebas

`GET /health` debe devolver `200` y `{"status":"ok"}`. Antes de publicar,
probar login, reserva, cancelación, reprogramación, panel y una consulta de Gemini.
