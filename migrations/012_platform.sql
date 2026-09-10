-- ============================================================
-- ETAPA SaaS - PLATAFORMA MULTI-TENANT / SUPERADMIN
-- ============================================================
--
-- Identidad de PLATAFORMA separada de la identidad de negocio:
--   - platform_users: cuenta del SUPERADMIN. No participa en business_users.
--   - platform_sessions: sesiones revocables del SUPERADMIN (mismo patrón que
--     `sessions` para usuarios de negocio, con namespace propio).
--   - audit_log: auditoría de acciones de plataforma (quién, qué, cuándo, IP).
--     NUNCA guarda contraseñas, tokens ni secretos.
--   - invitations: tokens de invitación (SÓLO hash SHA-256) para que el owner
--     establezca su contraseña. El plaintext solo existe en el email/enlace.
--
-- ALTER businesses.active: permite suspender/activar un negocio desde el panel
-- de plataforma. Default 1 para no alterar registros existentes (El Corte).

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS platform_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id INTEGER NOT NULL
        REFERENCES platform_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_platform_sessions_user
    ON platform_sessions (platform_user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id INTEGER
        REFERENCES platform_users(id) ON DELETE SET NULL,
    actor_email TEXT NOT NULL DEFAULT '',
    business_id INTEGER
        REFERENCES businesses(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_log_business_created
    ON audit_log (business_id, created_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log (action, created_at);

CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL
        REFERENCES businesses(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL
        REFERENCES users(id) ON DELETE CASCADE,
    role_name TEXT NOT NULL DEFAULT 'owner',
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_invitations_business
    ON invitations (business_id, created_at);

CREATE INDEX IF NOT EXISTS idx_invitations_user
    ON invitations (user_id);

ALTER TABLE businesses ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE businesses ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE businesses SET created_at = datetime('now') WHERE created_at = '';

COMMIT;

PRAGMA foreign_keys = ON;