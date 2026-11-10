from pathlib import Path
from unittest import mock

import pytest

from app import app

pytestmark = pytest.mark.no_pg


class TestBusinessLandingChat:
    def setup_method(self):
        self.client = app.test_client()

    def test_landing_uses_conversational_ui_and_slug_scoped_api_prefix(self):
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
            "content": {
                "welcome_label": "BIENVENIDO A {business_name}",
                "welcome_description": "Soy la recepción de {business_name}, {business_description}.",
            },
        }
        with (
            mock.patch("app.resolve_business", return_value=business),
            mock.patch("app.get_business_settings_scoped", return_value={}),
            mock.patch("app.build_public_frontend_config", return_value=frontend_config),
        ):
            response = self.client.get("/b/otro")

        assert response.status_code == 200
        assert b'data-api-prefix="/b/otro"' in response.data
        assert (
            b'data-message="\xc2\xbfQu\xc3\xa9 servicios tienen y cu\xc3\xa1nto cuestan?"'
            in response.data
        )
        assert b'data-message="Quiero reservar un turno"' in response.data
        assert b'data-message="Quiero consultar mis turnos"' in response.data
        assert b'id="message"' in response.data
        assert b'id="send"' in response.data
        assert response.data.count(b'class="quick-action"') == 4
        assert b"$12.345" not in response.data
        assert b"/static/app.js" in response.data
        assert b"/static/landing-chat.js" not in response.data
        assert b"$${ service.price }" not in response.data
        assert b'name="viewport"' in response.data

    def test_landing_renders_dynamic_content_from_frontend_config(self):
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
            "content": {
                "welcome_label": "BIENVENIDO A {business_name}",
                "welcome_description": "Soy la recepción de {business_name}, {business_description}.",
                "initial_message": "Hola dinámico 👋\n¿Cómo te ayudo?",
                "quick_actions": [
                    {"label": "Precios", "sub": "Ver precios", "message": "Quiero ver precios"},
                    {"label": "Agenda", "sub": "Ver agenda", "message": "Quiero ver agenda"},
                ],
            },
        }
        with (
            mock.patch("app.resolve_business", return_value=business),
            mock.patch("app.get_business_settings_scoped", return_value={}),
            mock.patch("app.build_public_frontend_config", return_value=frontend_config),
        ):
            response = self.client.get("/b/otro")
        assert response.status_code == 200
        assert "Hola dinámico 👋".encode() in response.data
        assert b'data-message="Quiero ver precios"' in response.data
        assert b'data-message="Quiero ver agenda"' in response.data
        assert b">Precios<" in response.data
        assert b">Agenda<" in response.data
        assert response.data.count(b'class="quick-action"') == 2

    def test_landing_escapes_initial_message_html_and_preserves_line_breaks(self):
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
            "content": {
                "initial_message": '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n<b>texto</b> "quotes"',
                "quick_actions": [
                    {
                        "label": "<b>L</b>",
                        "sub": "<img src=x onerror=1>",
                        "message": '<script>"x"</script>',
                    }
                ],
            },
        }
        with (
            mock.patch("app.resolve_business", return_value=business),
            mock.patch("app.get_business_settings_scoped", return_value={}),
            mock.patch("app.build_public_frontend_config", return_value=frontend_config),
        ):
            response = self.client.get("/b/otro")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "<script>alert(1)</script>" not in body
        assert "<img src=x onerror=alert(1)>" not in body
        assert "<b>texto</b>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "&lt;img src=x onerror=alert(1)&gt;" in body
        assert "&lt;b&gt;texto&lt;/b&gt; &#34;quotes&#34;" in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;<br>" in body
        assert "&lt;img src=x onerror=alert(1)&gt;<br>" in body
        assert 'data-message="&lt;script&gt;&#34;x&#34;&lt;/script&gt;"' in body
        assert "&lt;b&gt;L&lt;/b&gt;" in body
        assert "&lt;img src=x onerror=1&gt;" in body

    def test_conversational_frontend_keeps_argentine_currency_formatting(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "app.js"
        script = script_path.read_text(encoding="utf-8")

        assert "function formatARS(value)" in script
        assert 'amount.toLocaleString("es-AR"' in script
        assert "Number.isInteger(amount) ? 0 : 2" in script
        assert "formatARS(service.precio)" in script
        assert "const API_ROOT = `${API_PREFIX}/api`;" in script
        assert "chat: `${API_PREFIX}/chat`" in script
        assert "servicios: `${API_ROOT}/servicios`" in script
        assert "disponibilidad: `${API_ROOT}/disponibilidad`" in script
        assert "reservar: `${API_ROOT}/reservar`" in script
        assert "turnos: `${API_ROOT}/turnos`" in script
        assert "cancelar: `${API_ROOT}/cancelar`" in script
        assert "reprogramar: `${API_ROOT}/reprogramar`" in script

        assert "management_token=${encodeURIComponent(managementToken)}" not in script

        assert '"X-Management-Token": managementToken' in script

        assert "management_token: managementToken" in script
        assert "customer_name: name" in script
        assert "appointment-management-link" in script

    def test_conversational_layout_has_mobile_height_and_scrolling_constraints(self):
        stylesheet = Path(__file__).resolve().parents[1] / "static" / "style.css"
        css = stylesheet.read_text(encoding="utf-8")

        assert "@media (max-width: 600px)" in css
        assert "height: 100dvh;" in css
        assert "height: calc(100dvh - 42px);" in css
        assert "overflow: hidden;" in css
        assert "flex: 1 1 0;" in css

    def test_conversational_chat_posts_and_restores_history(self):
        script_path = Path(__file__).resolve().parents[1] / "static" / "app.js"
        script = script_path.read_text(encoding="utf-8")

        assert "fetchJSON(" in script
        assert "API.chat" in script
        assert "`${API.api}/conversations/current/messages`" in script
        assert "No pude recuperar la conversación anterior" in script
        assert "Ocurrió un problema al procesar tu consulta." in script

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
