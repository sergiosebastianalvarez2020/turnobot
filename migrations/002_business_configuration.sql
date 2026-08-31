-- Migración 002: configuración reutilizable del negocio

ALTER TABLE business_settings ADD COLUMN business_type TEXT NOT NULL DEFAULT 'Barbería';
ALTER TABLE business_settings ADD COLUMN business_initials TEXT NOT NULL DEFAULT 'EC';
ALTER TABLE business_settings ADD COLUMN business_description TEXT NOT NULL DEFAULT 'Barbería masculina';
ALTER TABLE business_settings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'America/Argentina/Buenos_Aires';
