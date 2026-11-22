-- Migracion 027: configuracion global de proveedores de IA (Super Admin).
--
-- Tabla de plataforma (no tenant-scoped): almacena la configuracion global
-- de proveedores de inteligencia artificial administrada exclusivamente
-- desde el Super Admin. Las credenciales se almacenan cifradas.
--
-- Compatible con SQLite y PostgreSQL.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS platform_ai_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL UNIQUE
        CHECK (provider IN ('gemini', 'groq', 'openrouter', 'cerebras')),
    api_key_encrypted TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 999,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_platform_ai_providers_priority
    ON platform_ai_providers (priority, active);

COMMIT;
