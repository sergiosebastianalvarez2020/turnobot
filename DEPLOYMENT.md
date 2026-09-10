# Despliegue

## Configuración

El archivo `.env` debe contener `FLASK_ENV=production`, `COOKIE_SECURE=1`,
`SECRET_KEY`, `ADMIN_PASSWORD_HASH` y `GEMINI_API_KEY`. Nunca debe publicarse.

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

## Gestión pública de turnos

Al crear un turno, la API devuelve un `management_token` aleatorio. Debe
conservarse y enviarse en el cuerpo POST para cancelar o reprogramar junto con
`appointment_id`. Solo se almacena su hash SHA-256.

Si el cliente NO presenta el `management_token`, para cancelar o reprogramar se
exige que coincidan EXACTAMENTE el nombre (`nombre`) y el teléfono (`telefono`)
del turno. El `appointment_id` por sí solo — incluso enumerando IDs — no
autoriza ninguna operación sobre turnos ajenos: sin token válido o sin el par
nombre+teléfono correcto, el turno no se modifica. Un tercero que conozca solo
el ID no puede cancelar ni reprogramar el turno de otra persona.

`/api/turnos`, la API pública y el asistente de IA exponen únicamente columnas
públicas del turno; `management_token_hash` nunca sale en las respuestas.

## CSRF

Los formularios de login, logout y todas las operaciones administrativas POST
requieren el campo oculto `csrf_token`, asociado a la sesión Flask. Las APIs
JSON públicas (`/chat`, reservas, cancelación y reprogramación) no requieren
CSRF porque no usan la sesión administrativa como autenticación; cancelación y
reprogramación exigen `management_token` o el par nombre+teléfono correcto. El
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

Con la aplicación detenida o en una tarea programada:

```powershell
venv\Scripts\python.exe scripts\backup_database.py
```

Restauración, con la aplicación detenida:

```powershell
venv\Scripts\python.exe scripts\restore_database.py database\backups\appointments-YYYYMMDD-HHMMSS.db
```

Conservar varias copias en otra unidad o servicio externo y probar la restauración periódicamente.

## Health check y pruebas

`GET /health` debe devolver `200` y `{"status":"ok"}`. Antes de publicar,
probar login, reserva, cancelación, reprogramación, panel y una consulta de Gemini.
