-- Migración 016: Base de conocimiento por negocio.
--
-- Permite a cada negocio almacenar información específica que la IA
-- debe conocer para responder a sus clientes (FAQ, instrucciones, políticas).
--
-- La búsqueda utiliza FTS5 para consultas simples por palabras clave.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE business_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('faq','instruction','policy')),
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    tags TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by_user_id INTEGER REFERENCES users(id)
);

CREATE INDEX idx_bk_business_type ON business_knowledge (business_id, type);
CREATE INDEX idx_bk_business_active ON business_knowledge (business_id, active);

-- FTS5 virtual table para búsqueda por palabras clave
CREATE VIRTUAL TABLE business_knowledge_fts USING fts5(
    question, answer, tags,
    content='business_knowledge',
    content_rowid='id'
);

-- Triggers para mantener sincronizada la tabla FTS
CREATE TRIGGER bk_fts_ai AFTER INSERT ON business_knowledge BEGIN
    INSERT INTO business_knowledge_fts(rowid, question, answer, tags)
    VALUES (new.id, new.question, new.answer, new.tags);
END;

CREATE TRIGGER bk_fts_ad AFTER DELETE ON business_knowledge BEGIN
    INSERT INTO business_knowledge_fts(business_knowledge_fts, rowid, question, answer, tags)
    VALUES ('delete', old.id, old.question, old.answer, old.tags);
END;

CREATE TRIGGER bk_fts_au AFTER UPDATE ON business_knowledge BEGIN
    INSERT INTO business_knowledge_fts(business_knowledge_fts, rowid, question, answer, tags)
    VALUES ('delete', old.id, old.question, old.answer, old.tags);
    INSERT INTO business_knowledge_fts(rowid, question, answer, tags)
    VALUES (new.id, new.question, new.answer, new.tags);
END;

COMMIT;

PRAGMA foreign_keys = ON;