import tempfile
import unittest
from pathlib import Path

import database.database as database
from services.conversations import (
    add_conversation_message_scoped,
    count_conversation_sessions_scoped,
    get_conversation_messages_by_public_token_scoped,
    get_conversation_messages_scoped,
    get_conversation_session_by_id_scoped,
    get_conversation_stats_scoped,
    get_frequent_questions_scoped,
    get_needs_human_sessions_scoped,
    get_or_create_conversation_session_scoped,
    get_or_create_public_conversation_session_scoped,
    get_unanswered_questions_scoped,
    list_conversation_sessions_scoped,
    request_human_handoff_scoped,
    resolve_human_handoff_scoped,
    track_question_scoped,
    update_session_status_scoped,
)


class TestConversations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        # Crear usuario owner para tests
        self.owner_id = database.create_user_scoped(
            "owner@test.com", database.generate_password_hash("testpass"), active=True
        )
        # Asignar rol owner al negocio 1
        connection = database.get_connection()
        try:
            owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
            connection.execute(
                "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
                (self.owner_id, 1, owner_role["id"]),
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_conversation_session_creation(self):
        """Crear sesión de conversación para un cliente."""
        session = get_or_create_conversation_session_scoped(
            1, "+5491112345678", "Juan Pérez", "juan@test.com"
        )
        self.assertIsNotNone(session)
        self.assertEqual(session["customer_phone"], "+5491112345678")
        self.assertEqual(session["customer_name"], "Juan Pérez")
        self.assertEqual(session["customer_email"], "juan@test.com")
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["needs_human"], 0)

    def test_conversation_session_isolation(self):
        """Sesiones aisladas por business_id."""
        # Crear negocio 2
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "Business 2", "business-2")'
            )
            owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
            connection.execute(
                "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
                (self.owner_id, 2, owner_role["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        session_a = get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session_b = get_or_create_conversation_session_scoped(2, "+5491112345678", "Pedro", "")

        self.assertNotEqual(session_a["id"], session_b["id"])
        self.assertEqual(session_a["business_id"], 1)
        self.assertEqual(session_b["business_id"], 2)

    def test_conversation_messages(self):
        """Agregar y obtener mensajes de una conversación."""
        session = get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session_id = session["id"]

        # Agregar mensajes
        add_conversation_message_scoped(session_id, 1, "user", "Hola, quiero un turno")
        add_conversation_message_scoped(session_id, 1, "assistant", "¡Hola! ¿Qué servicio buscas?")
        add_conversation_message_scoped(session_id, 1, "user", "Corte de pelo")

        messages = get_conversation_messages_scoped(session_id, 1)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "Hola, quiero un turno")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[2]["role"], "user")

    def test_human_handoff(self):
        """Solicitar y resolver atención humana."""
        session = get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session_id = session["id"]

        # Solicitar handoff
        result = request_human_handoff_scoped(session_id, 1)
        self.assertTrue(result)

        session = get_conversation_session_by_id_scoped(session_id, 1)
        self.assertEqual(session["status"], "needs_human")
        self.assertEqual(session["needs_human"], 1)
        self.assertIsNotNone(session["human_requested_at"])

        # Resolver handoff
        result = resolve_human_handoff_scoped(session_id, 1)
        self.assertTrue(result)

        session = get_conversation_session_by_id_scoped(session_id, 1)
        self.assertEqual(session["status"], "human_resolved")
        self.assertEqual(session["needs_human"], 0)
        self.assertIsNotNone(session["resolved_at"])

    def test_update_session_status(self):
        """Actualizar estado de sesión."""
        session = get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session_id = session["id"]

        result = update_session_status_scoped(session_id, 1, "closed")
        self.assertTrue(result)

        session = get_conversation_session_by_id_scoped(session_id, 1)
        self.assertEqual(session["status"], "closed")

    def test_list_sessions_with_filter(self):
        """Listar sesiones con filtro de estado."""
        get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session2 = get_or_create_conversation_session_scoped(1, "+5491123456789", "Pedro", "")
        session3 = get_or_create_conversation_session_scoped(1, "+5491134567890", "Maria", "")

        request_human_handoff_scoped(session2["id"], 1)
        resolve_human_handoff_scoped(session3["id"], 1)

        active = list_conversation_sessions_scoped(1, status="active")
        needs_human = list_conversation_sessions_scoped(1, status="needs_human")
        resolved = list_conversation_sessions_scoped(1, status="human_resolved")

        self.assertEqual(len(active), 1)
        self.assertEqual(len(needs_human), 1)
        self.assertEqual(len(resolved), 1)

    def test_count_sessions(self):
        """Contar sesiones por estado."""
        get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session2 = get_or_create_conversation_session_scoped(1, "+5491123456789", "Pedro", "")
        request_human_handoff_scoped(session2["id"], 1)

        total = count_conversation_sessions_scoped(1)
        active = count_conversation_sessions_scoped(1, status="active")
        needs_human = count_conversation_sessions_scoped(1, status="needs_human")

        self.assertEqual(total, 2)
        self.assertEqual(active, 1)
        self.assertEqual(needs_human, 1)

    def test_needs_human_sessions(self):
        """Obtener sesiones que requieren atención humana."""
        get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session2 = get_or_create_conversation_session_scoped(1, "+5491123456789", "Pedro", "")
        request_human_handoff_scoped(session2["id"], 1)

        needs_human = get_needs_human_sessions_scoped(1)
        self.assertEqual(len(needs_human), 1)
        self.assertEqual(needs_human[0]["customer_phone"], "+5491123456789")

    def test_track_question_analytics(self):
        """Registrar preguntas para analytics."""
        track_question_scoped(1, "Aceptan transferencia")
        track_question_scoped(1, "Aceptan transferencia")
        track_question_scoped(1, "Cuanto cuesta el corte")
        track_question_scoped(1, "Aceptan Mercado Pago", needs_human=1)

        frequent = get_frequent_questions_scoped(1, limit=10)
        self.assertEqual(len(frequent), 3)
        self.assertEqual(frequent[0]["question_text"], "Aceptan transferencia")
        self.assertEqual(frequent[0]["count"], 2)

        unanswered = get_unanswered_questions_scoped(1, limit=10)
        self.assertEqual(len(unanswered), 1)
        self.assertEqual(unanswered[0]["question_text"], "Aceptan Mercado Pago")
        self.assertEqual(unanswered[0]["needs_human_count"], 1)

    def test_conversation_stats(self):
        """Obtener estadísticas de conversaciones."""
        get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session2 = get_or_create_conversation_session_scoped(1, "+5491123456789", "Pedro", "")
        session3 = get_or_create_conversation_session_scoped(1, "+5491134567890", "Maria", "")
        request_human_handoff_scoped(session2["id"], 1)
        resolve_human_handoff_scoped(session3["id"], 1)

        stats = get_conversation_stats_scoped(1)
        self.assertEqual(stats["total_sessions"], 3)
        self.assertEqual(stats["needs_human"], 1)
        self.assertEqual(stats["resolved"], 1)

    def test_cross_tenant_isolation_analytics(self):
        """Analytics aislados por business_id."""
        # Crear negocio 2
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "Business 2", "business-2")'
            )
            owner_role = connection.execute("SELECT id FROM roles WHERE name = 'owner'").fetchone()
            connection.execute(
                "INSERT INTO business_users (user_id, business_id, role_id) VALUES (?, ?, ?)",
                (self.owner_id, 2, owner_role["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        track_question_scoped(1, "Pregunta A")
        track_question_scoped(2, "Pregunta B")

        freq_a = get_frequent_questions_scoped(1)
        freq_b = get_frequent_questions_scoped(2)

        self.assertEqual(len(freq_a), 1)
        self.assertEqual(freq_a[0]["question_text"], "Pregunta A")
        self.assertEqual(len(freq_b), 1)
        self.assertEqual(freq_b[0]["question_text"], "Pregunta B")

    def test_get_needs_human_sessions(self):
        """Obtener sesiones que requieren atención."""
        get_or_create_conversation_session_scoped(1, "+5491112345678", "Juan", "")
        session2 = get_or_create_conversation_session_scoped(1, "+5491123456789", "Pedro", "")
        request_human_handoff_scoped(session2["id"], 1)

        needs = get_needs_human_sessions_scoped(1)
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["customer_phone"], "+5491123456789")

    def test_public_session_creation(self):
        """Primera conversación crea sesión pública con public_token."""
        session = get_or_create_public_conversation_session_scoped(1)
        self.assertIsNotNone(session)
        self.assertIsNotNone(session.get("public_token"))
        self.assertEqual(session["business_id"], 1)
        self.assertTrue(session["customer_phone"].startswith("anon_"))

    def test_public_session_reuse(self):
        """Segunda petición con mismo negocio reusa última sesión pública."""
        session1 = get_or_create_public_conversation_session_scoped(1)
        session2 = get_or_create_public_conversation_session_scoped(1)
        self.assertEqual(session1["id"], session2["id"])
        self.assertEqual(session1["public_token"], session2["public_token"])

    def test_public_session_isolation_by_business(self):
        """Sesiones públicas aisladas por business_id."""
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "Business 2", "business-2")'
            )
            connection.commit()
        finally:
            connection.close()

        session1 = get_or_create_public_conversation_session_scoped(1)
        session2 = get_or_create_public_conversation_session_scoped(2)
        self.assertNotEqual(session1["id"], session2["id"])
        self.assertNotEqual(session1["public_token"], session2["public_token"])

    def test_get_messages_by_public_token(self):
        """Recuperar mensajes por public_token."""
        session = get_or_create_public_conversation_session_scoped(1)
        session_id = session["id"]
        public_token = session["public_token"]

        add_conversation_message_scoped(session_id, 1, "user", "Hola")
        add_conversation_message_scoped(
            session_id, 1, "assistant", "¡Hola! ¿En qué puedo ayudarte?"
        )

        messages = get_conversation_messages_by_public_token_scoped(public_token, 1)
        self.assertIsNotNone(messages)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "Hola")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_get_messages_by_public_token_wrong_business(self):
        """No se puede acceder a sesión de otro negocio por public_token."""
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "Business 2", "business-2")'
            )
            connection.commit()
        finally:
            connection.close()

        session = get_or_create_public_conversation_session_scoped(1)
        public_token = session["public_token"]

        messages = get_conversation_messages_by_public_token_scoped(public_token, 2)
        self.assertIsNone(messages)

    def test_get_messages_by_invalid_public_token(self):
        """Sesión inexistente devuelve None."""
        messages = get_conversation_messages_by_public_token_scoped("token_inexistente", 1)
        self.assertIsNone(messages)


if __name__ == "__main__":
    unittest.main()
