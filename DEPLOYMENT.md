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
`appointment_id`. Solo se almacena su hash SHA-256; el teléfono ya no es una
credencial para estas operaciones. Como transición limitada, los turnos antiguos
sin hash todavía aceptan el flujo anterior; este fallback debe eliminarse cuando
todos esos turnos hayan expirado, porque conserva el riesgo de teléfono + ID.

## CSRF

Los formularios de login, logout y todas las operaciones administrativas POST
requieren el campo oculto `csrf_token`, asociado a la sesión Flask. Las APIs
JSON públicas (`/chat`, reservas, cancelación y reprogramación) no requieren
CSRF porque no usan la sesión administrativa como autenticación; cancelación y
reprogramación usan `management_token`. Este token es independiente del token
CSRF y no debe confundirse con él.

## Provisioning

La creación de negocios se realiza actualmente mediante el comando controlado
`python scripts/provision_business.py "Nombre" email contraseña`. La operación
crea negocio, owner, membership, configuración y horarios iniciales dentro de
una única transacción. No se expone como endpoint público hasta contar con un
modelo de platform-admin, invitaciones y controles de abuso adecuados.

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
