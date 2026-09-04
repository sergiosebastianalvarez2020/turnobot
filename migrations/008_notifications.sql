-- Migración 008: Notificaciones y recordatorios de turnos.
--
-- 1) appointments.customer_email : correo del cliente que reserva, destino de
--    la confirmación y del recordatorio. Nullable para no romper turnos
--    históricos; en el flujo nuevo se captura al reservar.
--
-- 2) notification_log : registro idempotente de notificaciones enviadas para
--    evitar duplicados del runner y guardar el destino por tenant.
--
-- 3) business_settings.notifications_enabled : habilitación por negocio.

PRAGMA foreign_keys = OFF;

BEGIN;

ALTER TABLE appointments ADD COLUMN customer_email TEXT;

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL,
    business_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'email',
    destination TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (appointment_id) REFERENCES appointments(id),
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_log_unique
ON notification_log (business_id, appointment_id, type, channel);

CREATE INDEX IF NOT EXISTS idx_notification_log_business
ON notification_log (business_id);

ALTER TABLE business_settings ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 0;

COMMIT;

PRAGMA foreign_keys = ON;
