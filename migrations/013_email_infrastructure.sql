-- ============================================================
-- ETAPA 4 - INFRAESTRUCTURA DE EMAIL / SOLICITUDES DE ALTA
-- ============================================================
--
-- 1) businesses.pending: negocio en estado "solicitud recibida",
--    esperando aprobación del SUPERADMIN. Default 1 para registros
--    existentes (migrados a 0: se consideran ya aprobados, p.ej. El Corte).
-- 2) notification_log.status/error/last_attempt_at: estados de envío
--    (pending/sent/failed) + error técnico SANITIZADO (sin secretos)
--    para reintento sin colas de tareas.
-- 3) business_settings.notification_email: destinatario de los avisos
--    de nueva reserva POR NEGOCIO (EMAIL 5). Ej.
--    notifications_enabled + notification_email configuran el envío.

BEGIN IMMEDIATE;

ALTER TABLE businesses ADD COLUMN pending INTEGER NOT NULL DEFAULT 1;
UPDATE businesses SET pending = 0;

ALTER TABLE notification_log ADD COLUMN status TEXT NOT NULL DEFAULT 'sent';
ALTER TABLE notification_log ADD COLUMN error TEXT NOT NULL DEFAULT '';
ALTER TABLE notification_log ADD COLUMN last_attempt_at TEXT NOT NULL DEFAULT '';

ALTER TABLE business_settings ADD COLUMN notification_email TEXT NOT NULL DEFAULT '';

COMMIT;

PRAGMA foreign_keys = ON;