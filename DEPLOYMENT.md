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
