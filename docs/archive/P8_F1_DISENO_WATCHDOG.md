# P.8-F1 — Diseño congelado: observabilidad de disponibilidad (watchdog + alertas)

Estado: **DISEÑO APROBADO, NO IMPLEMENTADO**. F1 solo documenta. La implementación
será F2/F3, las pruebas controladas F4 y el despliegue en OCI F5. No se modificó
producción en OCI. No se ejecutó `git add/commit/push`.

## 0. Principios

- Conservador: reutiliza el patrón probado de P.6 (`OnFailure=turnobot-failure-notify@%N.service`).
- Cero dependencias nuevas (solo stdlib: `urllib.request`, `json`, socket sin secretos).
- Sin tocar: `Restart`/`RestartSec`/`StartLimit*` de `turnobot.service`, `/etc/turnobot.env`,
  `app.py`, BD, nginx, firewalld, `TRUSTED_PROXY_COUNT`, HTTPS/DNS/Caddy, `.gitignore`, `DEPLOYMENT.md`.
- Todo cambio en unidades se hace con **drop-ins** excepto los archivos que son nuevos en el repo
  (healthcheck), para que el rollback sea simple.

## 1. OnFailure para `turnobot.service`

**Archivo nuevo:** `deploy/systemd/turnobot.service.d/onfailure.conf`

```ini
[Unit]
OnFailure=turnobot-failure-notify@%N.service
```

- `%N` se expande al nombre de la unidad (`turnobot.service`).
- NO se modifican: `Restart=always`, `RestartSec=5s`, `StartLimitIntervalSec=10s`, `StartLimitBurst=5`.
- Efecto: si el proceso llega a `start-limit-hit`, `exit-code`, `timeout`, etc. (Result != success),
  se envía email de alerta. Hoy el servicio tiene `OnFailure=` vacío → caída silenciosa posible.

## 2. OnFailure para tareas programadas

**Archivos nuevos** (drop-ins, mismo patrón P.6):

| Unidad | Drop-in nuevo | Contenido |
|---|---|---|
| turnobot-backup-prune.service | `deploy/systemd/turnobot-backup-prune.service.d/onfailure.conf` | `[Unit]\nOnFailure=turnobot-failure-notify@%N.service` |
| turnobot-reminders.service | `deploy/systemd/turnobot-reminders.service.d/onfailure.conf` | ídem |
| turnobot-retry-notifications.service | `deploy/systemd/turnobot-retry-notifications.service.d/onfailure.conf` | ídem |

backup y backup-verify ya lo tienen (drop-in backup / inline verify); no se tocan.

## 3. Healthcheck HTTP

### 3.1 parámetros congelados

| Parámetro | Valor | Justificación |
|---|---|---|
| URL | `http://127.0.0.1:5000/health` | loopback local; no atraviesa nginx. Overridable con `--url` para tests/F4 |
| Código esperado | `200` | endpoint real (app.py:925) devuelve 200 solo con DB ok |
| Timeout por intento | `5s` | `urllib` timeout; cobertura de Waitress holgada |
| Intentos (máx) | `3` | garantiza ≥ 2 intentos fallidos consecutivos antes de error |
| Intervalo entre intentos | `10s` | > `RestartSec=5s` → evita falsos positivos durante reinicio |
| Exit 0 | únicamente si algún intento responde 200 | (json `{"status":"ok"...}` o al menos código 200) |
| Exit != 0 | si se agotan los intentos | código 1; summary de códigos/hora, sin secretos |
| Secreto | nunca se lee ni imprime | script no lee SMTP ni `.env` |

### 3.2 `scripts/check_health.py` (spec F2)

- stdlib únicamente. Firma: `--url` (default arriba), `--attempts` (default 3),
  `--timeout` (default 5), `--delay` (default 10).
- Lógica: loop `attempt in range(attempts)`; `GET` con timeout; si `status == 200` y JSON
  parseable → `print ok` → `return 0`. Si no, registrar fracaso (código o `timeout`), esperar
  `delay` (salvo último intento), continuar. Agotado → print resumen de fracasos por intento y
  devolver `1`. Excepciones (`URLError`, `TimeoutError`) se tratan como intento fallido.
- No imprime credenciales, no imprime body de respuesta, no usa headers de auth.
- `--url` y `--attempts <n>` permitirán que `tests/test_check_health.py` y la prueba de F4
  reduzcan reintentos y apunten a servidores de prueba en loopback.

### 3.3 unidades new de systemd (repo, para F3)

`deploy/systemd/turnobot-healthcheck.service`:

```ini
[Unit]
Description=TurnoBot - Healthcheck HTTP de disponibilidad
After=network.target

[Service]
Type=oneshot
User=opc
Group=opc
WorkingDirectory=/home/opc/proyecto/turnobot
EnvironmentFile=/etc/turnobot.env
Environment=PYTHONPATH=/home/opc/proyecto/turnobot
ExecStart=/opt/turnobot/venv312/bin/python /home/opc/proyecto/turnobot/scripts/check_health.py
NoNewPrivileges=true
PrivateTmp=true
```

OnFailure del healthcheck: **inline** en la misma unidad (es un archivo nuevo del repo, único
punto de verdad):
`OnFailure=turnobot-failure-notify@%N.service`.

`deploy/systemd/turnobot-healthcheck.timer`:

```ini
[Unit]
Description=TurnoBot - Healthcheck de disponibilidad (cada 5 min)

[Timer]
OnCalendar=*:0/5
Persistent=false
Unit=turnobot-healthcheck.service

[Install]
WantedBy=timers.target
```

| Decisión | Valor | Motivo |
|---|---|---|
| Frecuencia | `*:0/5` (cada 5 min) | detección de caída en ≤ ~10 min incl. reintentos; ruido bajo |
| `Persistent` | `false` | evita ráfaga de checks tras un arranque (evita falsos positivos) |
| `OnFailure` | notify@ (email) | solo alerta; NO reinicia (el reinicio ya lo hace `Restart=always` de turnobot) |
| `AccuracySec` | default | no es crítico subir precisión |

El timer solo corre si el servicio está levantado; si el proceso está caído, el OS ya lo
revive por `Restart=always` y este healthcheck alerta la indisponibilidad intermedia y los
`start-limit-hit` (que sin alerta serían silenciosos).

### 3.4 rollback exacto

1. `systemctl stop turnobot-healthcheck.timer`.
2. `sudo rm /etc/systemd/system/turnobot-healthcheck.service /etc/systemd/system/turnobot-healthcheck.timer`.
3. `sudo daemon-reload`.
4. (opcional) `systemctl reset-failed turnobot-healthcheck service y unidad de prueba de F4`.
- Los drop-ins OnFailure se revierten igual: borrar el `.d/onfailure.conf` y `daemon-reload`
  (AÇ±ade direccionado a fichero: sin cambios en la unidad base → rollback trivial).

## 4. Archivos previstos en Git (F2/F3; hoy NO se crean en OCI ni se commitea)

**Nuevos en `deploy/systemd/`:**
- `turnobot.service.d/onfailure.conf`
- `turnobot-backup-prune.service.d/onfailure.conf`
- `turnobot-reminders.service.d/onfailure.conf`
- `turnobot-retry-notifications.service.d/onfailure.conf`
- `turnobot-healthcheck.service`
- `turnobot-healthcheck.timer`

**Nuevo en `scripts/`:**
- `check_health.py`

**Nuevo en `tests/`:**
- `test_check_health.py`

**Modificados:** ninguno. `DEPLOYMENT.md` queda como está (cambio local preexistente, no se toca).

## 5. Seguridad (bloqueado)

No se toca: `/etc/turnobot.env`, credenciales SMTP, `app.py`, BD (incl. WAL/SHM), nginx,
firewalld, `TRUSTED_PROXY_COUNT`, HTTPS/DNS/Caddy, `.gitignore`, `DEPLOYMENT.md`.
No se instala ningún paquete. `check_health.py` no necesita secretos.

## 6. Procedimiento de prueba F4 (spec, no ejecutar ahora)

Contexto: todo en OCI, con rollback inmediato disponible.

1. **Fallo de oneshot con unidad desechable `/bin/false`**
   - Crear `/etc/systemd/system/turnobot-failure-test.service`:
     `Type=oneshot`, `User=opc`, `EnvironmentFile=/etc/turnobot.env`,
     `ExecStart=/bin/false`, `OnFailure=turnobot-failure-notify@%N.service`.
   - `systemctl daemon-reload` → `systemctl start turnobot-failure-test.service`.
   - Verificar `Result=exit-code`, email recibido en `EMAIL_FROM`, sin secretos en journal.
   - Limpieza: `systemctl reset-failed`, borrar el archivo, `daemon-reload`.
2. **Cadena OnFailure → notify@**: cubierta por el paso 1 (idéntica a la cadena de producción).
3. **Recuperación posterior**: `systemctl start turnobot`; verificar `ActiveState=active`,
   `/health` → 200, `/etc »` para (hecho) o `NRestarts` estable.
4. **Healthcheck ante servicio caído**: `systemctl stop turnobot`; correr manualmente
   `check_health.py --attempts 2 --delay 2` → esperar exit != 0. `systemctl start turnobot`;
   repetir → exit 0. (Todo manual, sin iniciar el timer todavía.)
5. **Healthcheck ante /health no-200**: dentro de los tests unitarios con un servidor HTTP de
   prueba en loopback que devuelve `500` (y variante 200-ok para el caso feliz). Además, en OCI,
   `check_health.py --url http://127.0.0.1:5000/noexiste` → exit != 0 (4xx).
6. **Ausencia de secreto**: test unitario que fuerza cadenas sensibles en stdout/stderr y
   verifica que no aparecen; revisión manual de journal en F4 (buscar `SMTP_PASSWORD`).

## 7. Criterios de alerta (anti fatiga)

- sistema: `OnFailure` solo con `Result != success`.
- watchdog: alerta solo tras ≥ 2 fallos consecutivos (3 intentos, intervalo 10 s > RestartSec).
- Deduplicación natural por `OnFailure` (una vez por activación de unidad caída).
- Enviar a un único destinatario (`EMAIL_FROM`) con asunto con nombre de unidad.