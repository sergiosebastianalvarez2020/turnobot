-- ============================================================
-- ETAPA 10 - PASSWORD RESET + STAFF INVITATIONS + BRANDING
-- ============================================================
--
-- 1) password_reset_tokens: tabla de tokens de recuperación de contraseña.
--    Separada de `invitations` (que tiene semántica de negocio/rol owner).
--    Un solo uso, hash SHA-256, vencimiento configurable por env.
-- 2) invitations.revoked_at: columna de auditoría para revocación.
-- 3) business_settings: columnas de branding (logo_url, primary_color,
--    secondary_color) scoped por negocio.

BEGIN IMMEDIATE;

-- Password reset tokens
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user
    ON password_reset_tokens (user_id);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash
    ON password_reset_tokens (token_hash);

-- Branding columns on business_settings
ALTER TABLE business_settings ADD COLUMN logo_url TEXT NOT NULL DEFAULT '';
ALTER TABLE business_settings ADD COLUMN primary_color TEXT NOT NULL DEFAULT '';
ALTER TABLE business_settings ADD COLUMN secondary_color TEXT NOT NULL DEFAULT '';

COMMIT;

PRAGMA foreign_keys = ON;
