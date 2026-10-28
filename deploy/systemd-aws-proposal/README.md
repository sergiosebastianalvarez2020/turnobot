# Propuesta systemd — Migración de schedulers y mantenimiento a AWS

**Estado:** PROPUESTA — nada de esto está instalado ni activado. Archivos nuevos, separados de
`deploy/systemd/` (plantillas OCI vigentes) y de las unidades ya instaladas en AWS.

**Destino:** AWS EC2 `18.191.0.23` (`ubuntu`), proyecto `/home/ubuntu/turnobot`, venv
`/home/ubuntu/turnobot/.venv312`.

## Contenido

| Tarea | Servicio | Timer | Horario (paridad con OCI) |
|---|---|---|---|
| Recordatorios diarios | `turnobot-reminders.service` | `turnobot-reminders.timer` | `*-*-* 13:00:00` UTC |
| Reintento notificaciones (15 min) | `turnobot-retry-notifications.service` | `turnobot-retry-notifications.timer` | `*:0/15` |
| Poda de backups PG | `turnobot-backup-prune.service` | `turnobot-backup-prune.timer` | `*-*-* 04:00:00` UTC |
| Verificación backup PG | `turnobot-backup-verify.service` | `turnobot-backup-verify.timer` | `Sun *-*-* 04:30:00` UTC |

## Diferencias respecto de las plantillas `deploy/systemd/` (OCI)

1. Rutas: `User/Group=ubuntu`, `WorkingDirectory=/home/ubuntu/turnobot`,
   `ExecStart=/home/ubuntu/turnobot/.venv312/bin/python`.
2. SMTP: los servicios de recordatorios y reintentos cargan **dos** EnvironmentFiles:
   `/etc/turnobot.env` (DATABASE_URL y config de app) **más** `/etc/turnobot-monitoring.env`
   (SMTP_*), porque en AWS los secretos SMTP están solo en el segundo. En OCI están en un solo
   archivo. Así **no se modifican los archivos de entorno existentes**.
3. Poda: se añade explícito `--dir /home/ubuntu/turnobot/backups_pg`. La plantilla OCI (y la
   unidad activa en OCI) corren sin `--dir`, con lo cual operan sobre `database/backups`
   (SQLite heredado) y **no podan** los dumps PostgreSQL (evidenciado en OCI: journal prune
   del 26/10 solo preserva, y `backups_pg` acumula 29 dumps).
4. `OnFailure=turnobot-failure-notify@%N.service` va inline en cada `[Unit]` (sin drop-ins),
   alineado con el template del healthcheck ya instalado en AWS.
5. Timer de poda: `After=network.target turnobot-backup.service` mantenido (paridad OCI).

## Comandos de instalación propuestos (NO ejecutar sin autorización)

```bash
# Copiar los 8 archivos a AWS (desde la máquina local; ajustar rutas)
scp -i '<clave AWS>' deploy/systemd-aws-proposal/* ubuntu@18.191.0.23:/tmp/proposal/

# En AWS (requiere sudo, previa revisión visual de cada archivo)
sudo cp /tmp/proposal/*.service /tmp/proposal/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# 1) Validación previa en frío con systemd-analyze (sin activar nada)
systemd-analyze verify /etc/systemd/system/turnobot-{reminders,retry-notifications,backup-prune,backup-verify}.{service,timer}

# 2) Prueba de una sola pasada de cada script SIN tocar scheduling (sudo del operador)
sudo -u ubuntu env $(cat /etc/turnobot.env | xargs) $(cat /etc/turnobot-monitoring.env | xargs) \
  /home/ubuntu/turnobot/.venv312/bin/python /home/ubuntu/turnobot/scripts/send_reminders.py     # dry: notifica y sale
sudo -u ubuntu /home/ubuntu/turnobot/.venv312/bin/python /home/ubuntu/turnobot/scripts/prune_backups.py \
  --dir /home/ubuntu/turnobot/backups_pg    # DRY-RUN por defecto: solo muestra plan

# 3) Activación (recién con aprobación explícita y ventana de cutover)
sudo systemctl enable --now turnobot-retry-notifications.timer turnobot-reminders.timer \
  turnobot-backup-prune.timer turnobot-backup-verify.timer

# 4) En cutover (con autorización): desactivar los 4 timers equivalentes en OCI
#    sudo systemctl disable --now turnobot-{reminders,retry-notifications}-{*}t* ...
```

## Riesgos cubiertos

- **Duplicación**: sin cron en ambos servidores ni schedulers in-process (verificado). Los
  recordatorios y reintentos son idempotentes por `notification_log` (claim + estado `sent`
  evita reenvío; reintentos solo procesan filas `failed` con `next_retry_at <= now`).
  El riesgo restante es el solape OCI+AWS tras la activación: la ventana debe cerrarse
  desactivando los timers OCI en el cutover (requiere autorización; incluida la decisión
  de si se repara el `PUBLIC_BASE_URL` de OCI o se apaga).
- **Pérdida de backups**: la poda nunca borra el dump más reciente ni los del día en curso,
  ignora symlinks, no sale del directorio, aborta si el plan supera 1000 borrados y deja
  PRESERVED todo lo que no matchee el patrón automático. Retención resultante: 7 diarios +
  4 semanales + 6 mensuales por formato.
- **Backoff de reintentos**: el intervalo real vive en BD (`next_retry_at` por fila,
  calculado con `retry_count`), el timer de 15 min solo actúa como poller — no reenvía
  más rápido que el backoff configurado.
