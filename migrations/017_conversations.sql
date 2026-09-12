-- Migración 017: Historial de conversaciones y escalamiento humano.
--
-- Permite almacenar el historial de conversaciones entre clientes y la IA,
-- detectar preguntas sin respuesta, y gestionar escalamientos a humano.

PRAGMA foreign_keys = OFF;

BEGIN;

-- ============================================================
-- SESIONES DE CONVERSACIÓN (una por cliente/negocio)
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_phone TEXT NOT NULL,
    customer_name TEXT,
    customer_email TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active, needs_human, human_resolved, closed
    needs_human INTEGER NOT NULL DEFAULT 0,
    human_requested_at TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, customer_phone)
);

CREATE INDEX idx_conv_sessions_business_status ON conversation_sessions (business_id, status);
CREATE INDEX idx_conv_sessions_business_phone ON conversation_sessions (business_id, customer_phone);

-- ============================================================
-- MENSAJES DE CONVERSACIÓN
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'human', 'system')),
    content TEXT NOT NULL,
    needs_human INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_conv_messages_session ON conversation_messages (session_id, created_at);
CREATE INDEX idx_conv_messages_needs_human ON conversation_messages (session_id, needs_human);

-- ============================================================
-- ANALYTICS: PREGUNTAS FRECUENTES (agregación simple por hash de pregunta normalizada)
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    question_hash TEXT NOT NULL,  -- hash simple de pregunta normalizada
    question_text TEXT NOT NULL,  -- texto representativo
    count INTEGER NOT NULL DEFAULT 1,
    needs_human_count INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, question_hash)
);

CREATE INDEX idx_conv_analytics_business ON conversation_analytics (business_id, count DESC);

COMMIT;

PRAGMA foreign_keys = ON;