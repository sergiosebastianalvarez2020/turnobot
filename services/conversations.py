"""Capa de conversaciones y escalamiento humano (scoped por business_id).

Proporciona helpers para:
- Gestionar sesiones de conversación por cliente/negocio
- Almacenar mensajes (user/assistant/human)
- Detectar y gestionar escalamientos a humano
- Analytics de preguntas frecuentes y sin respuesta
"""

import hashlib
import re
from database.database import get_connection


def _normalize_question(text):
    """Normaliza una pregunta para agrupación: minúsculas, sin puntuación, espacios."""
    text = text.strip().lower()
    text = re.sub(r'[¿?¡!.,;:]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def _question_hash(text):
    """Genera hash simple de pregunta normalizada."""
    normalized = _normalize_question(text)
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def get_or_create_conversation_session_scoped(business_id, customer_phone, customer_name=None, customer_email=None):
    """Obtiene o crea una sesión de conversación para un cliente en un negocio.

    La sesión se identifica por (business_id, customer_phone).
    """
    connection = get_connection()
    try:
        phone = (customer_phone or "").strip()
        if not phone:
            return None

        row = connection.execute(
            """
            SELECT id, business_id, customer_phone, customer_name, customer_email,
                   status, needs_human, human_requested_at, resolved_at, created_at, updated_at
            FROM conversation_sessions
            WHERE business_id = ? AND customer_phone = ?
            """,
            (business_id, phone),
        ).fetchone()

        if row:
            # Actualizar nombre/email si se proporcionaron y son nuevos
            updates = []
            params = []
            if customer_name and customer_name.strip() and row["customer_name"] != customer_name.strip():
                updates.append("customer_name = ?")
                params.append(customer_name.strip())
            if customer_email and customer_email.strip() and row["customer_email"] != customer_email.strip():
                updates.append("customer_email = ?")
                params.append(customer_email.strip().lower())

            if updates:
                params.extend([business_id, phone])
                connection.execute(
                    f"UPDATE conversation_sessions SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE business_id = ? AND customer_phone = ?",
                    params,
                )
                connection.commit()
            return _row_to_dict(row)

        # Crear nueva sesión
        cursor = connection.execute(
            """
            INSERT INTO conversation_sessions
                (business_id, customer_phone, customer_name, customer_email, status, needs_human)
            VALUES (?, ?, ?, ?, 'active', 0)
            """,
            (business_id, phone, customer_name, customer_email),
        )
        connection.commit()
        session_id = cursor.lastrowid
        return get_conversation_session_by_id_scoped(session_id, business_id)
    finally:
        connection.close()


def get_conversation_session_by_id_scoped(session_id, business_id):
    """Obtiene una sesión de conversación por ID, scoped por business_id."""
    connection = get_connection()
    try:
        row = connection.execute(
            """
            SELECT id, business_id, customer_phone, customer_name, customer_email,
                   status, needs_human, human_requested_at, resolved_at, created_at, updated_at
            FROM conversation_sessions
            WHERE id = ? AND business_id = ?
            """,
            (session_id, business_id),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        connection.close()


def list_conversation_sessions_scoped(business_id, status=None, limit=50, offset=0):
    """Lista sesiones de conversación de un negocio, con filtros opcionales."""
    connection = get_connection()
    try:
        query = """
            SELECT cs.id, cs.business_id, cs.customer_phone, cs.customer_name, cs.customer_email,
                   cs.status, cs.needs_human, cs.human_requested_at, cs.resolved_at,
                   cs.created_at, cs.updated_at,
                   (SELECT content FROM conversation_messages cm
                    WHERE cm.session_id = cs.id
                    ORDER BY cm.created_at DESC LIMIT 1) as last_message,
                   (SELECT COUNT(*) FROM conversation_messages cm
                    WHERE cm.session_id = cs.id AND cm.needs_human = 1) as unread_human_count
            FROM conversation_sessions cs
            WHERE cs.business_id = ?
        """
        params = [business_id]
        if status:
            query += " AND cs.status = ?"
            params.append(status)
        query += " ORDER BY cs.updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = connection.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def count_conversation_sessions_scoped(business_id, status=None):
    """Cuenta sesiones de conversación de un negocio."""
    connection = get_connection()
    try:
        query = "SELECT COUNT(*) FROM conversation_sessions WHERE business_id = ?"
        params = [business_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        return connection.execute(query, params).fetchone()[0]
    finally:
        connection.close()


def add_conversation_message_scoped(session_id, business_id, role, content, needs_human=0):
    """Añade un mensaje a una conversación."""
    connection = get_connection()
    try:
        # Verificar que la sesión pertenece al negocio
        session = connection.execute(
            "SELECT id FROM conversation_sessions WHERE id = ? AND business_id = ?",
            (session_id, business_id),
        ).fetchone()
        if not session:
            return None

        cursor = connection.execute(
            """
            INSERT INTO conversation_messages (session_id, role, content, needs_human)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, role, content, 1 if needs_human else 0),
        )
        # Actualizar timestamp de la sesión
        connection.execute(
            "UPDATE conversation_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_conversation_messages_scoped(session_id, business_id):
    """Obtiene todos los mensajes de una sesión."""
    connection = get_connection()
    try:
        # Verificar que la sesión pertenece al negocio
        session = connection.execute(
            "SELECT id FROM conversation_sessions WHERE id = ? AND business_id = ?",
            (session_id, business_id),
        ).fetchone()
        if not session:
            return []

        rows = connection.execute(
            """
            SELECT id, session_id, role, content, needs_human, created_at
            FROM conversation_messages
            WHERE session_id = ?
            ORDER BY created_at
            """,
            (session_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def request_human_handoff_scoped(session_id, business_id):
    """Marca una sesión como requiriendo atención humana."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE conversation_sessions
            SET status = 'needs_human', needs_human = 1,
                human_requested_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND business_id = ? AND status != 'human_resolved'
            """,
            (session_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def resolve_human_handoff_scoped(session_id, business_id, admin_user_id=None):
    """Marca una sesión como resuelta por humano."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE conversation_sessions
            SET status = 'human_resolved', needs_human = 0,
                resolved_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND business_id = ?
            """,
            (session_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_needs_human_sessions_scoped(business_id):
    """Obtiene sesiones que requieren atención humana."""
    return list_conversation_sessions_scoped(business_id, status='needs_human')


def update_session_status_scoped(session_id, business_id, status):
    """Actualiza el estado de una sesión."""
    connection = get_connection()
    try:
        cursor = connection.execute(
            """
            UPDATE conversation_sessions
            SET status = ?, needs_human = CASE WHEN ? = 'needs_human' THEN 1 ELSE 0 END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND business_id = ?
            """,
            (status, status, session_id, business_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# ============================================================
# ANALYTICS: PREGUNTAS FRECUENTES Y SIN RESPUESTA
# ============================================================

def track_question_scoped(business_id, question_text, needs_human=0):
    """Registra una pregunta para analytics (agrupación simple por hash)."""
    if not question_text or not question_text.strip():
        return
    connection = get_connection()
    try:
        q_hash = _question_hash(question_text)
        q_text = question_text.strip()[:500]
        connection.execute(
            """
            INSERT INTO conversation_analytics (business_id, question_hash, question_text, count, needs_human_count, last_seen_at)
            VALUES (?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(business_id, question_hash) DO UPDATE SET
                count = count + 1,
                needs_human_count = needs_human_count + ?,
                last_seen_at = CURRENT_TIMESTAMP,
                question_text = ?
            """,
            (business_id, q_hash, q_text, 1 if needs_human else 0, 1 if needs_human else 0, q_text),
        )
        connection.commit()
    finally:
        connection.close()


def get_frequent_questions_scoped(business_id, limit=20):
    """Obtiene las preguntas más frecuentes de un negocio."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT question_text, count, needs_human_count, last_seen_at
            FROM conversation_analytics
            WHERE business_id = ?
            ORDER BY count DESC
            LIMIT ?
            """,
            (business_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def get_unanswered_questions_scoped(business_id, limit=20):
    """Obtiene preguntas que la IA no pudo responder (needs_human > 0)."""
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT question_text, count, needs_human_count, last_seen_at
            FROM conversation_analytics
            WHERE business_id = ? AND needs_human_count > 0
            ORDER BY needs_human_count DESC, last_seen_at DESC
            LIMIT ?
            """,
            (business_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def get_conversation_stats_scoped(business_id):
    """Obtiene estadísticas básicas de conversaciones para un negocio."""
    connection = get_connection()
    try:
        total_sessions = connection.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE business_id = ?", (business_id,)
        ).fetchone()[0]
        total_messages = connection.execute(
            "SELECT COUNT(*) FROM conversation_messages cm JOIN conversation_sessions cs ON cm.session_id = cs.id WHERE cs.business_id = ?", (business_id,)
        ).fetchone()[0]
        needs_human = connection.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE business_id = ? AND needs_human = 1", (business_id,)
        ).fetchone()[0]
        resolved = connection.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE business_id = ? AND status = 'human_resolved'", (business_id,)
        ).fetchone()[0]
        answered_by_ai = total_sessions - needs_human
        resolution_rate = round((answered_by_ai / total_sessions * 100) if total_sessions > 0 else 0, 1)
        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "needs_human": needs_human,
            "resolved": resolved,
            "answered_by_ai": answered_by_ai,
            "resolution_rate": resolution_rate,
        }
    finally:
        connection.close()


def get_opportunities_scoped(business_id, limit=20):
    """
    Detecta oportunidades de mejora: preguntas frecuentes en analytics
    que NO tienen entrada en business_knowledge.
    """
    connection = get_connection()
    try:
        rows = connection.execute(
            """
            SELECT ca.question_text, ca.count, ca.needs_human_count, ca.last_seen_at
            FROM conversation_analytics ca
            LEFT JOIN business_knowledge bk
                ON bk.business_id = ca.business_id
                AND bk.active = 1
                AND (bk.question LIKE '%' || ca.question_text || '%' 
                     OR ca.question_text LIKE '%' || bk.question || '%')
            WHERE ca.business_id = ?
                AND bk.id IS NULL
            ORDER BY ca.count DESC, ca.needs_human_count DESC, ca.last_seen_at DESC
            LIMIT ?
            """,
            (business_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        connection.close()


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)