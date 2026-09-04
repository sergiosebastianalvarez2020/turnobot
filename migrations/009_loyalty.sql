-- Migración 009: sistema de fidelización por puntos (MVP Etapa 10.1).
--
-- 1) loyalty_settings      : configuración de fidelización POR negocio. Arranca
--    desactivada (enabled=0) para no alterar el comportamiento de los negocios
--    existentes. points_per_completed_appointment = puntos que otorga cada turno
--    completado (>= 0).
--
-- 2) loyalty_accounts      : una cuenta de fidelización por (business_id,
--    customer_phone_normalizado). El phone es el ANCLA ÚNICA de identidad (el
--    email y el nombre son auxiliares/snapshot). points_balance es el saldo
--    materializado, SIEMPRE reconstructible a partir del ledger.
--
-- 3) points_ledger         : libro mayor auditable e inmutable. Cada movimiento
--    permite saber business_id, account, delta, tipo, motivo, turno vinculado,
--    actor y fecha. El saldo se obtiene como SUM(delta).
--
-- Idempotencia: UNIQUE parcial sobre (business_id, appointment_id) para
-- movimientos type='earn', de modo que un turno completado solo puede acreditar
-- UNA vez a nivel de base de datos (protección ante reintentos y concurrencia).

PRAGMA foreign_keys = OFF;

BEGIN;

-- ============================================================
-- CONFIGURACIÓN DE FIDELIZACIÓN POR NEGOCIO
-- ============================================================
CREATE TABLE IF NOT EXISTS loyalty_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    points_per_completed_appointment INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

CREATE INDEX IF NOT EXISTS idx_loyalty_settings_business
    ON loyalty_settings (business_id);

-- ============================================================
-- CUENTAS DE FIDELIZACIÓN (una por business + phone normalizado)
-- ============================================================
CREATE TABLE IF NOT EXISTS loyalty_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    customer_phone TEXT NOT NULL,
    customer_email TEXT,
    customer_name TEXT,
    points_balance INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, customer_phone),
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

CREATE INDEX IF NOT EXISTS idx_loyalty_accounts_business_balance
    ON loyalty_accounts (business_id, points_balance DESC);

-- ============================================================
-- LIBRO MAYOR DE PUNTOS (auditable e inmutable)
-- ============================================================
CREATE TABLE IF NOT EXISTS points_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    type TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    appointment_id INTEGER,
    points_per_completed INTEGER,
    actor_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (business_id) REFERENCES businesses(id),
    FOREIGN KEY (account_id) REFERENCES loyalty_accounts(id),
    FOREIGN KEY (appointment_id) REFERENCES appointments(id),
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
);

-- Un turno completado solo puede acreditar UNA vez por tenant (idempotencia).
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_ledger_earn_per_appointment
    ON points_ledger (business_id, appointment_id)
    WHERE type = 'earn' AND appointment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_points_ledger_account
    ON points_ledger (account_id, id);

CREATE INDEX IF NOT EXISTS idx_points_ledger_business_created
    ON points_ledger (business_id, created_at);

COMMIT;

PRAGMA foreign_keys = ON;