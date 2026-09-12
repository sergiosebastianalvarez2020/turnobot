"""Capa de conocimiento del negocio (RAG simple por business_id).

Proporciona helpers scoped para gestionar y consultar la base de conocimiento
específica de cada negocio. Todas las operaciones están aisladas por business_id.
"""

from database.database import (
    get_connection,
)

TYPE_FAQ = "faq"
TYPE_INSTRUCTION = "instruction"
TYPE_POLICY = "policy"

VALID_TYPES = {TYPE_FAQ, TYPE_INSTRUCTION, TYPE_POLICY}


def _validate_type(type_):
    if type_ not in VALID_TYPES:
        raise ValueError(f"Tipo inválido: {type_}. Debe ser uno de: {', '.join(VALID_TYPES)}")


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def get_knowledge_scoped(business_id, active_only=True):
    """Devuelve todas las entradas de conocimiento de un negocio."""
    connection = get_connection()
    try:
        query = """
            SELECT id, business_id, type, question, answer, tags,
                   active, created_at, updated_at, created_by_user_id
            FROM business_knowledge
            WHERE business_id = ?
        """
        params = [business_id]
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY type, question"
        rows = connection.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def search_knowledge_scoped(business_id, query, limit=5):
    """Busca conocimiento relevante usando FTS5."""
    if not query or not query.strip():
        return []
    connection = get_connection()
    try:
        fts_query = query.strip()
        rows = connection.execute(
            """
            SELECT bk.id, bk.business_id, bk.type, bk.question, bk.answer,
                   bk.tags, bk.active, bk.created_at, bk.updated_at,
                   bk.created_by_user_id
            FROM business_knowledge_fts
            JOIN business_knowledge bk ON bk.id = business_knowledge_fts.rowid
            WHERE business_knowledge_fts MATCH ? AND bk.business_id = ? AND bk.active = 1
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, business_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def create_knowledge_scoped(business_id, type_, question, answer, tags, user_id):
    """Crea una nueva entrada de conocimiento para un negocio."""
    _validate_type(type_)
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO business_knowledge
                (business_id, type, question, answer, tags, active, created_by_user_id)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (business_id, type_, question, answer, tags, user_id),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_knowledge_scoped(knowledge_id, business_id, type_, question, answer, tags, active=None):
    """Actualiza una entrada de conocimiento existente."""
    _validate_type(type_)
    connection = get_connection()
    try:
        if active is not None:
            cursor = connection.execute(
                """
                UPDATE business_knowledge
                SET type = ?, question = ?, answer = ?, tags = ?, active = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND business_id = ?
                """,
                (type_, question, answer, tags, 1 if active else 0, knowledge_id, business_id),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE business_knowledge
                SET type = ?, question = ?, answer = ?, tags = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND business_id = ?
                """,
                (type_, question, answer, tags, knowledge_id, business_id),
            )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def delete_knowledge_scoped(knowledge_id, business_id):
    """Elimina una entrada de conocimiento."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            "DELETE FROM business_knowledge WHERE id = ? AND business_id = ?",
            (knowledge_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_knowledge_by_id_scoped(knowledge_id, business_id):
    """Obtiene una entrada de conocimiento por ID."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id, business_id, type, question, answer, tags,
                   active, created_at, updated_at, created_by_user_id
            FROM business_knowledge
            WHERE id = ? AND business_id = ?
            """,
            (knowledge_id, business_id),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        connection.close()