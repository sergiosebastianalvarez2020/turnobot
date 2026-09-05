-- Etapa 10.3: trazabilidad de acciones manuales de recuperacion.
-- La deteccion se calcula sobre appointments completados; no se crea customers.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS retention_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_phone TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('viewed', 'contacted', 'skipped')),
    notes TEXT,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_completed_date TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_retention_actions_business_created ON retention_actions (business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_retention_actions_phone ON retention_actions (business_id, customer_phone, created_at);
COMMIT;
PRAGMA foreign_keys = ON;
