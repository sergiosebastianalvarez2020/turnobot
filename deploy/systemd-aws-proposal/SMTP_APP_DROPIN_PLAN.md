# Plan — Corrección SMTP de la aplicación en AWS (drop-in), pendiente de aprobación

**Estado:** PROPUESTA. No se ha modificado AWS. Se requiere autorización para ejecutar los
pasos marcados con [AUTORIZACIÓN].

## 1. Situación verificada (evidencia)

- `/etc/systemd/system/turnobot.service` (Unit real, ejecutándose con éxito desde 23/10 11:35):
  - `EnvironmentFile=/etc/turnobot.env` (únicamente8 claves; ninguna SMTP).
  - Drop-in existente: `turnobot.service.d/onfailure.conf` (solo `OnFailure`).
- `/etc/turnobot-monitoring.env` (7 claves, todas DEFINIDAS): usado y probado por
  `turnobot-failure-notify@.service` (envío exitoso confirmado 26/10 03:03 UTC).
- `services/notifications.py`:
  - `smtp_configured()` = `bool(SMTP_HOST)` (56-57).
  - `_smtp_config()` lee SOLO `SMTP_HOST`, `SMTP_PORT` (def 587), `SMTP_USER`,
    `SMTP_PASSWORD`, `EMAIL_FROM` (fallback `SMTP_USER`), `EMAIL_FROM_NAME` (def `TurnoBot`)
    (60-68). **`ALERT_EMAIL` no se usa en la app** — solo en `notify_failure.py`.
  - Sin SMTP: `send_appointment_notification` retorna `smtp_not_configured` ANTES de
    escribir `notification_log` (líneas 304-305): sin ruta de reintento.
  - Con SMTP y fallo de envío: escribe fila `failed` con error SANITIZADO (sin
    `SMTP_PASSWORD`, una línea, ≤200 chars) y `next_retry_at` con backoff exponencial
    (base 15 min, tope 4 h) — determinado por `retry_delay_seconds()`.
- Solapamiento de claves entre los dos env: **NINGUNA** (verificado con `comm -12` en AWS).
  Fusión de `EnvironmentFile` sin ambigüedad; si hubiera conflicto, gana el último listado.

## 2. Cambio propuesto — drop-in único

Archivo NUEVO (no se listan secretos, solo la referencia al env existente):

`/etc/systemd/system/turnobot.service.d/smtp.conf`
```ini
[Service]
EnvironmentFile=/etc/turnobot-monitoring.env
```

- `systemd` concatena drop-ins alfabéticamente y aplica los `EnvironmentFile` en el orden
  listado por el resultado fusionado (`turnobot.env` del unit + `smtp.conf`): la app hereda
  `DATABASE_URL`/config actual **más** las 7 claves de mail. `ALERT_EMAIL` llega pero es
  inerte para la app. No se duplica ningún secreto (los valores siguen viviendo solo en
  `/etc/turnobot-monitoring.env`; el drop-in no los contiene).
- El parche es idéntico al mecanismo ya probado en `turnobot-backup.service.d/`.

[AUTORIZACIÓN] Comandos (en orden):
```bash
# Generar e instalar el drop-in
sudo tee /etc/systemd/system/turnobot.service.d/smtp.conf <<'EOF'
[Service]
EnvironmentFile=/etc/turnobot-monitoring.env
EOF

# Validar combinación fusionada y recargar config
systemctl cat turnobot.service                       # debe mostrar unit + 2 drop-ins
systemd-analyze verify /etc/systemd/system/turnobot.service   # rc=0, sin warnings
sudo systemctl daemon-reload
systemctl show turnobot.service -p EnvironmentFiles  # debe listar AMBOS archivos

# Aplicar al proceso
sudo systemctl restart turnobot.service
```
> El `daemon-reload` NO basta: solo al (re)iniciar la unidad se relee el `EnvironmentFile`.
> El reinicio es máquina local (waitress en 127.0.0.1) con `Restart=on-failure`;
> interrupción ~ segundos.

## 3. Pre-validación adicional (sin enviar correo, sin reiniciar lo que no toca)

- Chequeo de entorno en el contexto exacto del servicio, con una unidad onshot efímera
  que SOLO lee variables (no abre SMTP, no toca BD):
```bash
sudo systemd-run --collect --unit=turnobot-smtp-probe \
  --property=User=ubuntu \
  --property=WorkingDirectory=/home/ubuntu/turnobot \
  --property=EnvironmentFile=/etc/turnobot.env \
  --property=EnvironmentFile=/etc/turnobot-monitoring.env \
  /home/ubuntu/turnobot/.venv312/bin/python -c \
  'import os; from services import notifications as n; \
   print("smtp_configured:", n.smtp_configured()); \
   print("claves_presentes:", all(os.getenv(k) for k in ("SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASSWORD","EMAIL_FROM"))); \
   print("EMAIL_FROM_NAME_definida:", bool(os.getenv("EMAIL_FROM_NAME")))'
sudo journalctl -u turnobot-smtp-probe --no-pager | tail -5
```
  Salida esperada: `smtp_configured: True`, `claves_presentes: True`.
- Post-reinicio, verificación pasiva: `systemctl is-active`, `curl -s --max-time 5
  http://127.0.0.1:5000/health` (HTTP 200), y solo observar la próxima reserva real en
  journal (no se crean reservas de prueba ni correos de prueba):
  `journalctl -u turnobot.service -n 50`.

## 4. Riesgos identificados (reducidos por este plan)

1. **Interrupción del reinicio**: única ventana de downtime (segundos). No hay cambio de
   código ni de lógica, solo de entorno.
2. **Volumen de correo compartido**: alertas de infra y emails de negocio saldrán del
   mismo remitente SMTP (mismo `EMAIL_FROM`). Cuotas de envío de Gmail aplican
   (~500/día); la app solo manda emails por reserva activa (confirmaciones, recordatorios
   si se activan los schedulers).
3. **Fila en `notification_log` nueva**: al quedar SMTP configurado, la primera reserva
   real con email escribirá `notification_log` por primera vez en AWS. YA cubierto
   esquemáticamente (`upsert_notification_log_scoped`; sin migraciones nuevas: la tabla
   ya existe en el schema PG vigente).
4. **Sin solapamiento de claves**: fusión determinista; no altera ninguna var existente.
5. `turnobot-monitoring.env` es root-owned; systemd lo lee como root y lo inyecta a
   `ubuntu` — sin cambios de permisos.

## 5. Procedimiento de reversión

```bash
sudo rm /etc/systemd/system/turnobot.service.d/smtp.conf
sudo systemctl daemon-reload
sudo systemctl restart turnobot.service
curl -s --max-time 5 http://127.0.0.1:5000/health    # HTTP 200
```
La app vuelve exactamente al estado actual (`smtp_not_configured` se guarda y desaparece
la fila reintentable). Los registros `sent/failed` ya escritos en `notification_log` NO
se modifican ni eliminan con la reversión.

## 6. Requiere autorización explícita

1. Crear el drop-in `smtp.conf` en AWS (sudo).
2. `systemctl restart turnobot.service` (una vez tras el cambio; y otra si se revierte).
3. El chequeo efímero `systemd-run` de validación (crea una unidad transitoria, se
   limpia sola con `--collect`).
4. (Relacionados, ya en propuesta anterior) activar los schedulers de
   `deploy/systemd-aws-proposal/` y `notifications_enabled=1` del negocio: sin SMTP en
   la app y sin negocio habilitado, reminders/retry correrían —pero... recordatorios
   para negocios sin env habilitado no generan envío (comportamiento ya verificado
   en OCI, `resumen: 0 enviados`).
