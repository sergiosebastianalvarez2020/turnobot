-- Migración 014: consumo atómico de invitaciones y claims de email.
-- Compatible con 001..013; no elimina historial ni modifica datos existentes.
BEGIN IMMEDIATE;
ALTER TABLE invitations ADD COLUMN revoked_at TEXT;
ALTER TABLE notification_log ADD COLUMN claimed_at TEXT;
COMMIT;
