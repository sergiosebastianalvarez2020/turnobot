import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database.database as database
from app import app
from services.conversations import (
    add_conversation_message_scoped,
    get_or_create_public_conversation_session_scoped,
)


class TestChatRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "appointments.db"
        database.init_database()
        self.client = app.test_client()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_chat_returns_session_data(self):
        """POST /chat devuelve session_id y public_token."""
        with mock.patch("app.ask_ai", return_value=("Hola!", 1, "token-abc")):
            resp = self.client.post("/chat", json={"message": "hola"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertIn("session_id", data)
            self.assertIn("public_token", data)
            self.assertIsInstance(data["public_token"], str)
            self.assertGreater(len(data["public_token"]), 5)

    def test_chat_reuses_session_with_token(self):
        """Segundo mensaje con el mismo public_token reusa la sesión."""
        with mock.patch("app.ask_ai", return_value=("Hola!", 1, "token-abc")):
            resp1 = self.client.post("/chat", json={"message": "hola"})
            token = resp1.get_json()["public_token"]

        with mock.patch("app.ask_ai", return_value=("Todo bien", 1, token)):
            resp2 = self.client.post("/chat", json={"message": "como estas", "session_id": token})
            data = resp2.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["public_token"], token)

    def test_public_messages_endpoint(self):
        """GET /api/conversations/<token>/messages devuelve el historial."""
        session = get_or_create_public_conversation_session_scoped(1)
        session_id = session["id"]
        public_token = session["public_token"]

        add_conversation_message_scoped(session_id, 1, "user", "Hola")
        add_conversation_message_scoped(session_id, 1, "assistant", "¡Hola!")

        resp = self.client.get(f"/api/conversations/{public_token}/messages")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["role"], "user")
        self.assertEqual(data["messages"][0]["content"], "Hola")

    def test_public_messages_endpoint_wrong_business(self):
        """No se puede recuperar historial de otro negocio."""
        connection = database.get_connection()
        try:
            connection.execute(
                'INSERT INTO businesses (id, name, slug) VALUES (2, "other", "other")'
            )
            connection.commit()
        finally:
            connection.close()

        session = get_or_create_public_conversation_session_scoped(1)
        public_token = session["public_token"]

        with mock.patch("app.get_current_business_id", return_value=2):
            resp = self.client.get(f"/api/conversations/{public_token}/messages")
            self.assertEqual(resp.status_code, 404)

    def test_public_messages_endpoint_invalid_token(self):
        """Token inexistente devuelve 404."""
        resp = self.client.get("/api/conversations/token-inexistente/messages")
        self.assertEqual(resp.status_code, 404)

    def test_chat_without_business_returns_error(self):
        """Sin contexto de negocio, /chat devuelve error amigable."""
        resp = self.client.get("/chat")
        self.assertEqual(resp.status_code, 405)


if __name__ == "__main__":
    unittest.main()
