BEGIN IMMEDIATE;

-- Identidad visual multi-tenant (Fase identidad).
-- Una fila por negocio (UNIQUE). `config` es un JSON con el set de colores,
-- tipografía y media del tenant; `locked_options` es un JSON array con los
-- campos que el Super Admin bloqueó para el tenant.
CREATE TABLE business_visual_identity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL UNIQUE REFERENCES businesses(id) ON DELETE CASCADE,
    config TEXT NOT NULL DEFAULT '{}',
    locked_options TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_business_visual_identity_business
    ON business_visual_identity (business_id);

COMMIT;
