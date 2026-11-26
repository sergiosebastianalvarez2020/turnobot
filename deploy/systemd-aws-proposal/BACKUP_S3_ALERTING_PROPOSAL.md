# Propuesta de alertas y observabilidad para backups S3

**Estado:** propuesta. No se activa en producción ni se envían notificaciones reales.

## Señales emitidas por `scripts/upload_s3_backup.py`

| Señal | Significado | Acción sugerida |
|---|---|---|
| `UPLOAD_OK dump=...` | Dump subido a S3. | Informativo. |
| `UPLOAD_OK sidecar=...` | Sidecar `.sha256` subido. | Informativo. |
| `LOCAL_SHA256_OK ...` | Hash local no cambió durante la subida. | Informativo. |
| `UPLOAD_MARKER_OK ...` | Marcador local de pendiente confirmado. | Informativo. |
| `REMOTE_VERIFICATION=OK` | Head, tamaño, metadata, descarga, SHA-256 y `pg_restore --list` remotos OK. | Informativo. |
| `S3_RETRY ...` | Error transitorio reintentado. | Warning si se repite. |
| `S3_ERROR ...` | Error de subida/verificación o fallo de un archivo pendiente. | Alerta. |
| `S3_UPLOAD_PENDING=0` | No hay pendientes. | Informativo. |
| `S3_UPLOAD_SUMMARY ...` | Resumen de lote. | Alerta si `failed > 0`. |

## Códigos de salida relevantes

- `2`: uso/argumentos inválidos.
- `3`: archivo inválido, ausente, symlink, nombre/tamaño no válido.
- `4`: `pg_restore --list` local falló.
- `5`: hash/sidecar inconsistente o archivo local cambió.
- `6`: subida S3 fallida.
- `7`: verificación remota fallida.
- `8`: no se pudo escribir el marcador local tras verificación remota OK.

## Reglas de alerta recomendadas

1. **Fallo de unidad**: `OnFailure=turnobot-failure-notify@%N.service` ya configurado; mantiene la ruta de alerta existente.
2. **Pendientes acumulados**: si `S3_UPLOAD_PENDING` o la ausencia de `.uploaded` supera 24 h para el dump automático más reciente, generar alerta operativa.
3. **IAM/permisos**: clasificar `AccessDenied`, `InvalidAccessKeyId`, `SignatureDoesNotMatch` como fallo no transitorio y alertar inmediatamente.
4. **Bucket inexistente/región**: clasificar `NoSuchBucket`, `PermanentRedirect`, `AuthorizationHeaderMalformed` como configuración inválida; no reintentar más allá de la política actual.
5. **Conectividad**: `EndpointConnectionError`/timeouts son transitorios con backoff; alertar solo si agotan reintentos o si el fallo persiste en ejecuciones consecutivas.
6. **Verificación remota**: cualquier `REMOTE_VERIFY_FAIL` o salida `7` debe alertar con severidad alta; no borrar ni marcar el backup como confirmado.
7. **Marcador local**: salida `8` indica backup remoto verificado pero estado local no persistido; requiere intervención antes de la siguiente ejecución para evitar reprocesos ambiguos.

## Verificación operativa propuesta (sin ejecutar ahora)

```bash
systemd-analyze verify \
  /etc/systemd/system/turnobot-backup-s3.service \
  /etc/systemd/system/turnobot-backup-s3-recovery.service \
  /etc/systemd/system/turnobot-backup-s3-recovery.timer \
  /etc/systemd/system/turnobot-backup.service.d/s3-upload.conf
systemctl status turnobot-backup-s3.service --no-pager
journalctl -u turnobot-backup-s3.service -n 200 --no-pager
find /home/ubuntu/turnobot/backups_pg -name 'turnobot-pg-*.dump' -type f | sort
find /home/ubuntu/turnobot/state/s3_backup_uploads -name '*.uploaded' -type f | sort
```

## Límites

- No se configura CloudWatch/SNS/IAM ni se crean recursos AWS en esta tarea.
- No se envían correos, SMS ni notificaciones reales.
- La evidencia operativa final requiere ejecución autorizada en AWS.