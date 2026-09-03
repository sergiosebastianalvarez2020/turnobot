-- Habilita una fila de configuración independiente por negocio.
PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE business_settings_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name TEXT DEFAULT 'Mi negocio',
    slot_duration INTEGER NOT NULL DEFAULT 60,
    break_between_slots INTEGER NOT NULL DEFAULT 0,
    business_type TEXT NOT NULL DEFAULT 'Barberia',
    business_initials TEXT NOT NULL DEFAULT 'EC',
    business_description TEXT NOT NULL DEFAULT 'Barberia masculina',
    timezone TEXT NOT NULL DEFAULT 'America/Argentina/Buenos_Aires',
    business_id INTEGER NOT NULL,
    UNIQUE (business_id),
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

INSERT INTO business_settings_new
    (id, business_name, slot_duration, break_between_slots, business_type,
     business_initials, business_description, timezone, business_id)
SELECT id, business_name, slot_duration, break_between_slots, business_type,
       business_initials, business_description, timezone, business_id
FROM business_settings;

DROP TABLE business_settings;
ALTER TABLE business_settings_new RENAME TO business_settings;

COMMIT;
PRAGMA foreign_keys = ON;
