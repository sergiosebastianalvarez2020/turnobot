from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from app import app

pytestmark = pytest.mark.no_pg


class TestBusinessLandingChat:
    def setup_method(self):
        self.client = app.test_client()

    def test_landing_formats_prices_and_uses_slug_scoped_chat_routes(self):
        business = {"id": 2, "name": "Otro", "slug": "otro"}
        frontend_config = {
            "business": {
                "name": "Otro",
                "type": "Barbería",
                "description": "Un negocio",
                "timezone": "America/Argentina/Buenos_Aires",
                "initials": "OT",
            },
            "branding": {"logo_url": "", "primary_color": "#123456", "secondary_color": "#234567"},
        }
        with (
            mock.patch("app.resolve_business", return_value=business),
            mock.patch("app.get_business_settings_scoped", return_value={}),
            mock.patch(
                "app.get_active_services_scoped",
                return_value=[
                    {"name": "Corte", "price": 12345, "duration": 30},
                    {"name": "Barba", "price": Decimal("12345.50"), "duration": 20},
                ],
            ),
            mock.patch("app.build_public_frontend_config", return_value=frontend_config),
        ):
            response = self.client.get("/b/otro")

        assert response.status_code == 200
        assert b"$12.345" in response.data
        assert b"$12.345,5" in response.data
        assert b'data-chat-url="/b/otro/chat"' in response.data
        assert b'data-history-url="/b/otro/api/conversations/current/messages"' in response.data
        assert b"/static/landing-chat.js" in response.data
        assert b"$${ service.price }" not in response.data
        assert b'name="viewport"' in response.data
        assert b"@media (max-width: 600px)" in response.data
        assert b"width: calc(100vw - 24px)" in response.data

    def test_chat_widget_opens_posts_and_surfaces_errors(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "landing-chat.js"
        script = script_path.read_text(encoding="utf-8")

        assert 'toggle.addEventListener("click", openChat)' in script
        assert "panel.hidden = false" in script
        assert "fetch(panel.dataset.chatUrl" in script
        assert 'appendMessage("error"' in script
        assert "response.ok" in script

    def test_chat_and_history_are_scoped_to_the_requested_business(self):
        businesses = {
            "barberia-a": {"id": 2, "name": "Barbería A", "slug": "barberia-a"},
            "barberia-b": {"id": 3, "name": "Barbería B", "slug": "barberia-b"},
        }
        received_business_ids = []

        def fake_ask_ai(_message, _conversation, *, business_id, **_kwargs):
            received_business_ids.append(business_id)
            return "Respuesta", 1, f"token-{business_id}"

        clients = {"barberia-a": app.test_client(), "barberia-b": app.test_client()}
        with (
            mock.patch("app.resolve_business", side_effect=businesses.get),
            mock.patch("app.ask_ai", side_effect=fake_ask_ai),
            mock.patch("app.is_chat_request_allowed", return_value=True),
            mock.patch(
                "routes.public_api.get_conversation_messages_by_public_token_scoped",
                side_effect=lambda token, business_id: [
                    {"role": "user", "content": f"{token}:{business_id}"}
                ],
            ) as get_messages,
        ):
            for slug, business_id in (("barberia-a", 2), ("barberia-b", 3)):
                response = clients[slug].post(
                    f"/b/{slug}/chat", json={"message": "Hola", "conversation": []}
                )
                assert response.status_code == 200
                assert response.get_json()["response"] == "Respuesta"
                with clients[slug].session_transaction() as cookie_session:
                    assert cookie_session["anonymous_chat_tokens"][str(business_id)] == (
                        f"token-{business_id}"
                    )

                history = clients[slug].get(f"/b/{slug}/api/conversations/current/messages")
                assert history.status_code == 200
                assert history.get_json()["messages"] == [
                    {"role": "user", "content": f"token-{business_id}:{business_id}"}
                ]

        assert received_business_ids == [2, 3]
        assert [call.args for call in get_messages.call_args_list] == [
            ("token-2", 2),
            ("token-3", 3),
        ]

    def test_unknown_business_chat_route_returns_not_found(self):
        with mock.patch("app.resolve_business", return_value=None):
            response = self.client.post("/b/unknown/chat", json={"message": "Hola"})

        assert response.status_code == 404

    def test_assistant_error_is_reported_without_claiming_success(self):
        business = {"id": 2, "name": "Otro", "slug": "otro"}
        with (
            mock.patch("app.resolve_business", return_value=business),
            mock.patch("app.is_chat_request_allowed", return_value=True),
            mock.patch("app.ask_ai", side_effect=RuntimeError("AI provider unavailable")),
        ):
            response = self.client.post("/b/otro/chat", json={"message": "Hola"})

        assert response.status_code == 500
        assert response.get_json() == {
            "success": False,
            "error": "No se pudo procesar la consulta.",
        }
