# Plan de instalación de schedulers en AWS — FASE 2 (pendiente de autorización)

**Estado:** PROPUESTA. Validado el 27/10 en AWS y OCI sin instalar nada en producción.
`/tmp/proposal/` en AWS contiene copias de estos archivos para verificación; NADA en
`/etc/systemd/system/` ni timers activados.

## 1. Resultados de validación (evidencia)

| Ítem | Resultado |
|---|---|
| `systemd-analyze verify` (8 archivos, en `/tmp/proposal`, sin instalar) | 8/8 OK, sin warnings propios |
| Scripts remotos sin CRLF | OK |
| Poda: **DRY-RUN real en AWS** (`prune_backups.py --dir backups_pg`) | rc=0, "0 keep, 0 delete, 4 PRESERVED", no borra nada |
| Verificación backup: **ejecución real read-only** (`verify_backup_postgresql.py`) | rc=0, `RESULTADO: OK` sobre `turnobot-pg-20261026-033015.dump` vía `pg_restore --list` (timeout 120 s, nunca restaura sobre producción) |
| `notification_log` en AWS (SELECT read-only) | **SIN filas** — borrón y cuenta nueva: no hay reintentos pendientes |
| `notification_log` en OCI (SELECT read-only) | **SIN filas** — el retry de OCI no puede reenviar nada (riesgo de doble envío vía reintentos ≈ nulo) |
| Recordatorios OCI | Habilitación de negocio en `notifications_enabled=0` → "0 enviados" (no hay doble envío actual) |
| Cron | Solo `certbot`/`e2scrub_all`; sin duplicadores de TurnoBot en AWS ni OCI |
| Limit de reintentos | `--limit 100` correcto (CLI); backoff real en BD (`next_retry_at`, base 15 min, tope 4 h) y tope `MAX_NOTIFICATION_RETRIES = 5` (`database/database.py:22`) |
| Idempotencia | `claim`/`already_sent` en `notification_log`; reintentos solo si el turno sigue `confirmed` y del negocio correcto |

## 2. PRERREQUISITO crítico descubierto: sincronizar scripts en AWS

- El `prune_backups.py` desplegado en AWS (commit `5efb354`) es la **versión solo-SQLite**
  (`BACKUP_NAME_RE` solo `appointments-`). El dry-run lo confirma: los dumps PG quedan
  PRESERVED → **la poda sería un no-op seguro pero inútil** (nunca libera espacio, no
  hay riesgo de pérdida de backup con esta versión).
- Las versiones PG-aware de `scripts/prune_backups.py` y `retry_failed_notifications.py`
  están en el worktree local PERO **sin commit** (`git status: M` ambos).
- **Antes de instalar `turnobot-backup-prune`**, se necesita: commit del fix local +
  deploy del script nuevo en AWS (pull o copia del archivo). Sin eso, el timer de poda
  solo imprimirá PRESERVED (inofensivo, pero no cumple su función).

## 3. Secuencia de implementación propuesta (con autorización)

```bash
# --- En el repo local (requiere commit con autorización separada) ---
git add scripts/prune_backups.py scripts/retry_failed_notifications.py
git commit -m "ops: prune PG-aware y retry alineados con pool CLI"
git push

# --- En AWS ---
# 1) sincronizar código (también propone pull para otros scripts/alerts ya desplegados)
cd /home/ubuntu/turnobot && git pull

# 2) colocar unidades (desde /tmp/proposal ya copiadas y verificadas)
sudo cp /tmp/proposal/*.service /tmp/proposal/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# 3) validar combinaciones instaladas
systemd-analyze verify /etc/systemd/system/turnobot-{backup-prune,backup-verify,reminders,retry-notifications}.{service,timer}

# 4) tras cada git pull: repetir DRY-RUN para confirmar que el prune ya detecta los dumps
.venv312/bin/python scripts/prune_backups.py --dir /home/ubuntu/turnobot/backups_pg
#    Esperado tras sync: todos los dumps del patrón PG en KEEP (7 días) y PRESERVED solo los raros

# 5) activación por fases (orden recomendado):
#    a) backups (riesgo mínimo, no envía email):
sudo systemctl enable --now turnobot-backup-verify.timer turnobot-backup-prune.timer
#    b) reintento (solo actúa sobre filas failed; tabla vacía ahora):
sudo systemctl enable --now turnobot-retry-notifications.timer
#    c) recordatorios (recién con decisión del operador sobre notifications_enabled):
sudo systemctl enable --now turnobot-reminders.timer
```

## 4. Reversión (orden inverso)

```bash
sudo systemctl disable --now turnobot-reminders.timer turnobot-retry-notifications.timer \
  turnobot-backup-prune.timer turnobot-backup-verify.timer
sudo rm /etc/systemd/system/turnobot-{reminders,retry-notifications,backup-prune,backup-verify}.{service,timer}
sudo systemctl daemon-reload
# Limpieza opcional: rm -rf /tmp/proposal
```
La reversión no toca `notification_log` ni backups ni env files.

## 5. Doble envío OCI/AWS y `notifications_enabled` (decisión del operador)

- Hoy: OCI `notification_log` vacío + negocio con notificaciones deshabilitadas
  → OCI no puede duplicar correos aunque siga con timers activos.
- Recomendación: activar en AWS y **recién en el cutover** desactivar los 4 timers OCI
  (`sudo systemctl disable --now turnobot-{reminders,retry-notifications,backup-prune,backup-verify}.timer`
  en OCI) — requiere autorización separada específica para OCI.
- `notifications_enabled=1` para el negocio es decisión de producto del operador; hasta
  entonces reminders/retry correrán sin efecto (logs "0 enviados" / "No hay notificaciones
  fallidas pendientes").

## 6. Riesgos pendientes

1. **Scripts desactualizados en AWS** (prune/retry) — bloquea funcionalidad de poda;
   requiere commit + pull (autorización).
2. Dirección git de AWS con `static/app.js` modificado y artefactos sin seguimiento
   (`backups_pg/` untracked, `.bak`): un `git pull` con conflicto en `static/app.js`
   requerirá resolución manual — planificar en ventana.
3. Primer run de reminders con negocio habilitado enviará correos reales a clientes
   (no hay staging): revisar en la primera ventana journal + `notification_log` tras el
   run de las 13:00 UTC.
