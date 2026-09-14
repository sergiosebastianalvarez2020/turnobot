-- Migración 019: token público para recuperación de conversación.
--
-- Agrega un campo public_token a conversation_sessions para permitir
-- la recuperación del historial desde el frontend sin exponer IDs secuenciales.

PRAGMA foreign_keys = OFF;

BEGIN;

ALTER TABLE conversation_sessions ADD COLUMN public_token TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_sessions_public_token
    ON conversation_sessions (public_token)
    WHERE public_token IS NOT NULL;

COMMIT;

PRAGMA foreign_keys = ON;
