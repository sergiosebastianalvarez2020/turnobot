import unittest
from types import SimpleNamespace
from unittest import mock

import database.database as database
from app import app
from services import ai
from services.conversations import (
    add_conversation_message_scoped,
    get_conversation_messages_by_public_token_scoped,
    get_or_create_public_conversation_session_scoped,
)


class TestChatRecovery(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_chat_does_not_expose_anonymous_session_bearer(self):
        """La identidad anónima queda en la cookie firmada del servidor."""
        with mock.patch("app.ask_ai", return_value=("Hola!", 1, "token-abc")):
            resp = self.client.post("/chat", json={"message": "hola"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertNotIn("session_id", data)
            self.assertNotIn("public_token", data)
            with self.client.session_transaction() as cookie_session:
                token = cookie_session["anonymous_chat_tokens"]["1"]
            self.assertEqual(token, "token-abc")

    def test_chat_reuses_session_with_token(self):
        """La cookie firmada conserva la sesión aunque el cliente altere session_id."""
        received_tokens = []

        def persist_message(message, _history, business_id, public_token=None, **_kwargs):
            received_tokens.append(public_token)
            current = get_or_create_public_conversation_session_scoped(
                business_id, visitor_token=public_token
            )
            add_conversation_message_scoped(current["id"], business_id, "user", message)
            return "ok", current["id"], current["public_token"]

        with mock.patch("app.ask_ai", side_effect=persist_message):
            first = self.client.post("/chat", json={"message": "hola"})
            with self.client.session_transaction() as cookie_session:
                token = cookie_session["anonymous_chat_tokens"]["1"]
            second = self.client.post(
                "/chat", json={"message": "seguimiento", "session_id": "token-ajeno"}
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIsNone(received_tokens[0])
        self.assertEqual(received_tokens[1], token)
        history = get_conversation_messages_by_public_token_scoped(token, 1)
        self.assertEqual([item["content"] for item in history], ["hola", "seguimiento"])

    def test_ask_ai_preserves_cookie_token_across_messages(self):
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))],
            text="Respuesta",
            usage_metadata=None,
        )
        with (
            app.app_context(),
            mock.patch("services.ai._call_gemini_with_retry", return_value=(response, 1, None)),
        ):
            first = ai.ask_ai("primer mensaje", business_id=1)
            second = ai.ask_ai("segundo mensaje", business_id=1, public_token=first[2])

        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2], second[2])
        messages = get_conversation_messages_by_public_token_scoped(first[2], 1)
        self.assertEqual(
            [item["content"] for item in messages],
            ["primer mensaje", "Respuesta", "segundo mensaje", "Respuesta"],
        )

    def test_two_visitors_and_businesses_have_cookie_scoped_histories(self):
        visitor_a = app.test_client()
        visitor_b = app.test_client()

        def persist_message(message, _history, business_id, public_token=None, **_kwargs):
            current = get_or_create_public_conversation_session_scoped(
                business_id, visitor_token=public_token
            )
            add_conversation_message_scoped(current["id"], business_id, "user", message)
            return "ok", current["id"], current["public_token"]

        with mock.patch("app.ask_ai", side_effect=persist_message):
            visitor_a.post("/chat", json={"message": "historial A"})
            with visitor_a.session_transaction() as cookie_session:
                token_a = cookie_session["anonymous_chat_tokens"]["1"]
            visitor_b.post("/chat", json={"message": "historial B"})
            with visitor_b.session_transaction() as cookie_session:
                token_b = cookie_session["anonymous_chat_tokens"]["1"]

            self.assertNotEqual(token_a, token_b)
            own_a = visitor_a.get("/api/conversations/current/messages").get_json()["messages"]
            own_b = visitor_b.get("/api/conversations/current/messages").get_json()["messages"]
            self.assertEqual([item["content"] for item in own_a], ["historial A"])
            self.assertEqual([item["content"] for item in own_b], ["historial B"])
            self.assertEqual(
                visitor_a.get(f"/api/conversations/{token_b}/messages").status_code, 404
            )
            self.assertEqual(
                visitor_b.get(f"/api/conversations/{token_a}/messages").status_code, 404
            )

            connection = database.get_connection()
            try:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug) VALUES (2, 'other', 'other')"
                )
                connection.commit()
            finally:
                connection.close()
            with mock.patch("app.get_current_business_id", return_value=2):
                cross_business = visitor_a.get("/api/conversations/current/messages")
            self.assertEqual(cross_business.status_code, 200)
            self.assertEqual(cross_business.get_json()["messages"], [])

    def test_anonymous_cookie_uses_secure_flask_cookie_policy(self):
        with mock.patch("app.ask_ai", return_value=("Hola!", 1, "token-abc")):
            response = self.client.post("/chat", json={"message": "hola"})
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        if app.config["SESSION_COOKIE_SECURE"]:
            self.assertIn("Secure", cookie)

    def test_public_messages_endpoint(self):
        """GET /api/conversations/<token>/messages devuelve el historial."""
        session = get_or_create_public_conversation_session_scoped(1)
        session_id = session["id"]
        public_token = session["public_token"]

        add_conversation_message_scoped(session_id, 1, "user", "Hola")
        add_conversation_message_scoped(session_id, 1, "assistant", "¡Hola!")

        with mock.patch("app.ask_ai", return_value=("continuación", session_id, public_token)):
            self.client.post("/chat", json={"message": "continuación"})

        resp = self.client.get(f"/api/conversations/{public_token}/messages")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["messages"][0]["role"], "user")
        self.assertEqual(data["messages"][0]["content"], "Hola")

        current = self.client.get("/api/conversations/current/messages")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.get_json()["messages"], data["messages"])

    def test_public_messages_endpoint_wrong_business(self):
        """No se puede recuperar historial de otro negocio."""
        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT INTO businesses (id, name, slug) VALUES (2, 'other', 'other')"
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
