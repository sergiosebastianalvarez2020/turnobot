# Propuesta systemd — Migración de schedulers y mantenimiento a AWS

**Estado:** PROPUESTA — nada de esto está instalado ni activado. Archivos nuevos, separados de
`deploy/systemd/` (plantillas OCI vigentes) y de las unidades ya instaladas en AWS.

**Destino:** AWS EC2 `18.191.0.23` (`ubuntu`), proyecto `/home/ubuntu/turnobot`, venv
`/home/ubuntu/turnobot/.venv312`.

**Validación en CI:** El commit `4720b37` (rama `recovery/local-improvements-20261124`)
pasa GitHub Actions #218 con Python 3.12 y PostgreSQL 16. **CI aprobado no significa
integración en main ni despliegue en AWS.** La instalación en AWS requiere autorización
explícita y ventana de cutover.

## Contenido

| Tarea | Servicio | Timer | Horario (paridad con OCI) |
|---|---|---|---|
| Recordatorios diarios | `turnobot-reminders.service` | `turnobot-reminders.timer` | `*-*-* 13:00:00` UTC |
| Reintento notificaciones (15 min) | `turnobot-retry-notifications.service` | `turnobot-retry-notifications.timer` | `*:0/15` |
| Poda de backups PG | `turnobot-backup-prune.service` | `turnobot-backup-prune.timer` | `*-*-* 04:00:00` UTC |
| Verificación backup PG | `turnobot-backup-verify.service` | `turnobot-backup-verify.timer` | `Sun *-*-* 04:30:00` UTC |
| Subida backup PG a S3 | `turnobot-backup-s3.service` | drop-in `ExecStartPost` en `turnobot-backup.service` | tras backup local exitoso; reintenta pendientes por marcadores |

## Subida S3 de backups (propuesta actualizada)

- `turnobot-backup-s3.service` usa el venv de producción:
  `/home/ubuntu/turnobot/.venv312/bin/python`.
- `scripts/upload_s3_backup.py` usa `boto3.client.upload_file()` con
  `TransferConfig(multipart_threshold=64MiB, multipart_chunksize=16MiB,
  max_concurrency=4, use_threads=True)`, evitando cargar el dump completo en RAM.
- El servicio selecciona dumps válidos `turnobot-pg-YYYYMMDD-HHMMSS.dump`,
  ordena por más reciente primero, omite los ya confirmados por marcador en
  `/home/ubuntu/turnobot/state/s3_backup_uploads/<dump>.uploaded`, continúa con
  los pendientes si uno falla y sale con error si quedó algún fallo.
- El marcador local solo se escribe después de verificación remota y
  confirmación del hash local final.
- `TimeoutStartSec=3600` cubre dumps grandes (~2 GB) en EC2 con ancho de banda
  variable. Ajustar solo con evidencia.
- Alertas: `OnFailure=turnobot-failure-notify@%N.service` en la unidad y
  marcadores de log documentados en `BACKUP_S3_ALERTING_PROPOSAL.md`.

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

## Análisis de concurrencia systemd Type=oneshot

**Escenario**: `turnobot-backup.service` (timer diario 03:30 UTC, `Type=oneshot`) dispara
`turnobot-backup-s3.service` (también `Type=oneshot`) vía `ExecStartPost=+/bin/bash -c
'/bin/systemctl start --no-block turnobot-backup-s3.service || true'`.

**Riesgo de solape**: Si la subida S3 del día anterior no ha terminado cuando se ejecuta el
backup del día siguiente, se iniciarían dos instancias concurrentes de
`turnobot-backup-s3.service`.

**Fuentes de concurrencia** (distintas, no equivalentes):

1. **Reinicio repetido de la misma unidad systemd**: `systemctl start` sobre una unidad
   `Type=oneshot` ya en ejecución devuelve error inmediato (`Job ... already running`);
   no lanza una segunda instancia. El riesgo real viene de `ExecStartPost` con
   `--no-block` que dispara el servicio S3 sin esperar; si el timer diario dispara
   `turnobot-backup.service` mientras la subida S3 anterior sigue viva, systemd **sí**
   permite la segunda ejecución porque son dos invocaciones separadas de la unidad S3.

2. **Ejecuciones externas independientes**: Otro proceso (cron, script manual, otra
   unidad) que invoque `upload_s3_backup.py` directamente con el mismo `--state-dir`.

3. **Ejecuciones manuales directas del script**: Operador que corre
   `python scripts/upload_s3_backup.py ...` fuera de systemd.

`Type=oneshot` **no genera por sí mismo** ejecuciones concurrentes; la concurrencia
solo ocurre si hay múltiples disparadores independientes (timer + `ExecStartPost` sin
esperar, o procesos externos).

**Mitigaciones existentes**:
1. **Marcadores atómicos**: `scripts/upload_s3_backup.py` usa archivos `.uploaded` con
   `os.replace()` atómico. Cada proceso construye su lista de pendientes comprobando
   `is_upload_done()` (lee y valida el marcador: SHA-256 64 hex + `s3://` URI) antes de subir.
2. **Idempotencia de contenido**: El marcador contiene `SHA-256  s3://...`. Si dos
   procesos suben el mismo archivo (mismo hash), escriben el mismo marcador; la
   segunda escritura `os.replace()` es segura y no corrompe el estado.
3. **Ventana realista**: Backups diarios a las 03:30 UTC; subida típica < 10 min
   (dumps ~100–500 MB, multipart 4 hilos, us-east-2). Probabilidad de solape > 24 h:
   despreciable salvo degradación severa de red o S3.

**Riesgo residual**: *Race condition* entre `is_upload_done()` y `mark_uploaded()`.
Dos procesos concurrentes podrían:
- Ambos leer `is_upload_done() == False` para el mismo dump
- Ambos subirlo a S3 (duplicado de ancho de banda y tiempo)
- Ambos escribir el mismo marcador (seguro por `os.replace()`)

**Decisión**: **No añadir `flock` a nivel de servicio** en esta versión.
- Complejidad añadida (lock file, cleanup en señales, `ExecStartPre`/`ExecStopPost`)
- Bajo impacto real: duplicación de subida eventual, sin pérdida de datos ni corrupción
- Los marcadores garantizan *eventual consistency* y reentrancia segura

**Recomendación futura**: Si se detecta solape real (logs `S3_UPLOAD_PENDING` duplicados
consecutivos), añadir `ExecStartPre=/usr/bin/flock -n /run/lock/turnobot-s3-upload.lock`
al servicio S3, o migrar a `systemd-run --property=ExecStartPre=...` con lock exclusivo.
Por ahora se documenta el riesgo y se monitorizan los logs.

## Permisos IAM requeridos (instancia EC2 vía instance profile)

El script `scripts/upload_s3_backup.py` y el servicio `turnobot-backup-s3.service` corren
bajo el instance profile de la EC2 `18.191.0.23`. **No se usan claves estáticas** (`~/.aws/`,
variables de entorno `AWS_ACCESS_KEY_ID`, etc.).

### Permisos mínimos (policy inline o managed policy adjunta al role de la instancia)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TurnoBotBackupS3Upload",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:AbortMultipartUpload"
      ],
      "Resource": "arn:aws:s3:::turnobot-backups-528120377808-us-east-2-an/postgres/*"
    }
  ]
}
```

### Justificación de cada permiso

| Acción | Uso en `upload_s3_backup.py` |
|---|---|
| `s3:PutObject` | `client.upload_file()` para dump y sidecar `.sha256` (multipart internamente) |
| `s3:GetObject` | `client.download_file()` en `verify_remote()` para descargar y validar hash + `pg_restore --list`; `client.head_object()` usa internamente `GetObject` |
| `s3:AbortMultipartUpload` | Limpieza de uploads multipart fallidos/parciales |

### Restricciones de seguridad

- **Resource ARN**: Restringido a `postgres/*` dentro del bucket específico. No acceso a otros
  prefijos ni buckets.
- **Sin `s3:DeleteObject`**: El script no borra objetos. La retención/limpieza se gestiona
  externamente (políticas de ciclo de vida S3 o jobs separados).
- **Sin `s3:ListBucket`**, `s3:GetBucketLocation`, `s3:ListAllMyBuckets`, etc.: No necesarios para la operación.
- **Sin `s3:PutObjectTagging`**: El script no envía etiquetas; se omite para reducir superficie.

### SSE-KMS (si aplica)

Si el bucket usa **SSE-KMS** (clave KMS gestionada por AWS o clave del cliente), añadir:

```json
{
  "Sid": "TurnoBotBackupKMS",
  "Effect": "Allow",
  "Action": [
    "kms:GenerateDataKey",
    "kms:Decrypt"
  ],
  "Resource": "arn:aws:kms:us-east-2:528120377808:key/<key-id>",
  "Condition": {
    "StringEquals": {
      "kms:ViaService": "s3.us-east-2.amazonaws.com"
    }
  }
}
```

Para **SSE-S3** (clave gestionada por S3), no se requieren permisos KMS adicionales.

### Verificación en AWS (tras despliegue autorizado)

```bash
# 1) Simular permisos con IAM Policy Simulator (consola AWS → IAM → Policy Simulator)
#    Role: arn:aws:iam::528120377808:role/<instance-profile-role>
#    Acciones: PutObject, GetObject, AbortMultipartUpload
#    Recurso: arn:aws:s3:::turnobot-backups-528120377808-us-east-2-an/postgres/turnobot-pg-20261124-033000.dump

# 2) Prueba funcional (como usuario ubuntu en la EC2)
aws s3api head-object --bucket turnobot-backups-528120377808-us-east-2-an --key postgres/2026/11/turnobot-pg-20261124-033000.dump
aws s3 cp /tmp/test.dump s3://turnobot-backups-528120377808-us-east-2-an/postgres/2026/11/test.dump
aws s3 cp s3://turnobot-backups-528120377808-us-east-2-an/postgres/2026/11/test.dump /tmp/test.verify
diff /tmp/test.dump /tmp/test.verify && echo "OK"
```

### Alertas IAM relacionadas

Ver `BACKUP_S3_ALERTING_PROPOSAL.md` §3: `AccessDenied`, `InvalidAccessKeyId`,
`SignatureDoesNotMatch` → alerta inmediata (no transitorio). `NoSuchBucket`,
`PermanentRedirect` → configuración inválida.
