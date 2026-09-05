-- ============================================================
-- ETAPA 10.2 - RECOMPENSAS Y CANJES
-- ============================================================
--
-- rewards: catalogo de recompensas por negocio.
--   - UNIQUE(business_id, name) evita duplicados de catalogo por tenant.
--   - points_cost CHECK (> 0). SQLite no valida CHECK en datos legacy,
--     por eso la validacion fuerte vive en el backend (services/loyalty.py).
--   - NO se eliminan fisicamente (se desactivan) para preservar el
--     historial de canjes (redemptions.reward_id).
--
-- redemptions: historial de canjes.
--   - FKs a businesses / loyalty_accounts / rewards / users.
--   - CHECK points_used > 0.
--   - idempotency_key: UUID generado al renderizar el formulario de canje
--     del cliente. Absorbe doble-click / refresh / reintento HTTP / dos
--     solicitudes simultaneas: un segundo intento con la misma clave es
--     absorbido y devuelve el canje original sin descuento adicional. Dos
--     canjes legitimos usan claves distintas (nueva clave por formulario) y
--     se registran normalmente. UNIQUE + transaccion = robustez real.
--   - points_used: foto del costo al momento del canje (el costo de la
--     recompensa puede cambiar en el futuro).

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL
        REFERENCES businesses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    points_cost INTEGER NOT NULL CHECK (points_cost > 0),
    active INTEGER NOT NULL DEFAULT 1
        CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, name)
);

ALTER TABLE points_ledger ADD COLUMN reward_id INTEGER REFERENCES rewards(id);
ALTER TABLE points_ledger ADD COLUMN redemption_id INTEGER;

CREATE TABLE IF NOT EXISTS redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL
        REFERENCES businesses(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL
        REFERENCES loyalty_accounts(id) ON DELETE CASCADE,
    reward_id INTEGER NOT NULL
        REFERENCES rewards(id) ON DELETE CASCADE,
    points_used INTEGER NOT NULL
        CHECK (points_used > 0),
    status TEXT NOT NULL DEFAULT 'redeemed'
        CHECK (status IN ('redeemed', 'cancelled')),
    idempotency_key TEXT NOT NULL,
    actor_user_id INTEGER
        REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_redemptions_business_created
    ON redemptions (business_id, created_at);

CREATE INDEX IF NOT EXISTS idx_redemptions_account
    ON redemptions (account_id, created_at);

COMMIT;

PRAGMA foreign_keys = ON;
