-- Migración 005: modelo de usuarios y multitenant administrativo.
--
-- Agrega users, roles, business_users (N:N user<->business con rol) y
-- sessions (expiración/revocación). No elimina datos existentes.
-- El seed del usuario owner inicial del negocio 1 se realiza en el
-- arranque de la aplicación a partir de ADMIN_PASSWORD_HASH / ADMIN_EMAIL
-- (ver database/seed_auth.py), por lo que aquí solo se define el esquema
-- y los roles base.

PRAGMA foreign_keys = OFF;
BEGIN;

-- ============================================================
-- USUARIOS
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- ROLES
-- ============================================================
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

INSERT INTO roles (id, name) VALUES
    (1, 'owner'),
    (2, 'admin'),
    (3, 'staff'),
    (4, 'customer')
ON CONFLICT(name) DO NOTHING;

-- ============================================================
-- RELACIÓN USER <-> BUSINESS CON ROL
-- ============================================================
CREATE TABLE IF NOT EXISTS business_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    business_id INTEGER NOT NULL DEFAULT 1,
    role_id INTEGER NOT NULL,
    UNIQUE (user_id, business_id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (business_id) REFERENCES businesses(id),
    FOREIGN KEY (role_id) REFERENCES roles(id)
);

CREATE INDEX IF NOT EXISTS idx_business_users_business
    ON business_users (business_id);
CREATE INDEX IF NOT EXISTS idx_business_users_user
    ON business_users (user_id);

-- ============================================================
-- SESIONES
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions (user_id);

COMMIT;

PRAGMA foreign_keys = ON;