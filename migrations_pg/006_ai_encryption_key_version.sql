-- Migracion 006: agrega versionado de clave de cifrado a proveedores de IA.
--
-- Agrega la columna encryption_key_version a platform_ai_providers para
-- soportar rotacion segura de AI_ENCRYPTION_KEY.

ALTER TABLE platform_ai_providers
    ADD COLUMN encryption_key_version INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_platform_ai_providers_key_version
    ON platform_ai_providers (encryption_key_version);
