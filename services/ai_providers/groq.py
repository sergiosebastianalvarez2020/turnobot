"""
Proveedor Groq para TurnoBot.
Compatible con la interfaz AIProvider.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from services.ai_providers.base import AIProvider

load_dotenv()

_GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
_GROQ_CHAT_ENDPOINT = f"{_GROQ_BASE_URL}/chat/completions"
_GROQ_TIMEOUT_S = float(os.getenv("GROQ_TIMEOUT_S", "8"))


class _GeminiCompatibleResponse:
    """Envuelve una respuesta de Groq para que parezca una respuesta de Gemini."""

    def __init__(self, text: str | None, function_calls: list[dict], usage: dict | None):
        self._text = text
        self._function_calls = function_calls
        self._usage = usage or {}

    @property
    def text(self) -> str:
        return self._text or ""

    @property
    def candidates(self) -> list:
        parts: list[Any] = []
        for fc in self._function_calls:
            parts.append(_FunctionCallPart(fc))
        if self._text and not self._function_calls:
            parts.append(_TextPart(self._text))
        return [type("Candidate", (), {"content": type("Content", (), {"parts": parts})})()]

    @property
    def usage_metadata(self) -> dict:
        return self._usage


class _FunctionCallPart:
    def __init__(self, fc: dict):
        self.function_call = _FunctionCall(fc)


class _FunctionCall:
    def __init__(self, fc: dict):
        self.name = fc.get("name", "")
        self.args = fc.get("arguments", {})


class _TextPart:
    def __init__(self, text: str):
        self.text = text


class GroqProvider(AIProvider):
    """Adaptador de Groq bajo la interfaz AIProvider."""

    _provider_key = "groq"
    _key_env = "GROQ_API_KEY"
    _model_env = "GROQ_MODEL"
    _default_model = "llama-3.1-8b-instant"
    _timeout_s = _GROQ_TIMEOUT_S
    _chat_endpoint = _GROQ_CHAT_ENDPOINT
    _log_name = "turnobot.ai.groq"

    def provider_name(self) -> str:
        return self._provider_key

    def is_configured(self) -> bool:
        return bool(os.getenv(self._key_env))

    # ------------------------------------------------------------------
    # Conversión de formatos Gemini <-> OpenAI
    # ------------------------------------------------------------------

    @staticmethod
    def _contents_to_messages(contents: Any) -> list[dict]:
        """Convierte tipos.Content de Gemini a mensajes de OpenAI."""
        messages: list[dict] = []
        for content in contents or []:
            role = getattr(content, "role", "user")
            # Gemini usa "model"; OpenAI usa "assistant"
            if role == "model":
                role = "assistant"
            parts = getattr(content, "parts", []) or []
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for part in parts:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_calls.append(
                        {
                            "id": getattr(fc, "id", None)
                            or f"call_{__import__('uuid').uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": fc.name,
                                "arguments": __import__("json").dumps(fc.args or {}),
                            },
                        }
                    )
                else:
                    text = getattr(part, "text", None)
                    if text:
                        text_parts.append(text)
            # OpenAI permite content + tool_calls en el mismo mensaje
            message: dict[str, Any] = {"role": role}
            if text_parts:
                message["content"] = "\n".join(text_parts)
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
        return messages

    @staticmethod
    def _declarations_to_tools(tools: Any) -> list[dict] | None:
        """Convierte types.Tool (Gemini) a formato de herramientas OpenAI."""
        if not tools:
            return None
        openai_tools = []
        for tool in tools:
            fds = getattr(tool, "function_declarations", None) or []
            for fd in fds:
                openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": fd.name,
                            "description": fd.description or "",
                            "parameters": fd.parameters_json_schema
                            or {"type": "object", "properties": {}, "required": []},
                        },
                    }
                )
        return openai_tools or None

    @staticmethod
    def _config_to_openai(config: Any) -> dict:
        """Convierte GenerateContentConfig de Gemini a parámetros de OpenAI."""
        result: dict[str, Any] = {}
        if config is None:
            return result
        system = getattr(config, "system_instruction", None)
        if system:
            result["system"] = system
        temp = getattr(config, "temperature", None)
        if temp is not None:
            result["temperature"] = temp
        max_tokens = getattr(config, "max_output_tokens", None)
        if max_tokens is not None:
            result["max_tokens"] = max_tokens
        return result

    @staticmethod
    def _extract_function_calls(message: dict) -> list[dict]:
        """Extrae llamadas a función de un mensaje de respuesta de OpenAI."""
        calls = []
        tool_calls = message.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append({"id": tc.get("id", "call_0"), "name": name, "arguments": args})
        return calls

    @staticmethod
    def _extract_text(message: dict) -> str | None:
        """Extrae el texto de un mensaje de respuesta de OpenAI."""
        return message.get("content")

    # ------------------------------------------------------------------
    # Clasificación de errores
    # ------------------------------------------------------------------

    _GROQ_BASE_URL = _GROQ_BASE_URL
    _GROQ_CHAT_ENDPOINT = _GROQ_CHAT_ENDPOINT
    _GROQ_TIMEOUT_S = _GROQ_TIMEOUT_S
    _TRANSIENT_STATUSES = {"DEADLINE_EXCEEDED", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL"}

    @staticmethod
    def _classify_http_error(status_code: int | None) -> tuple[str, bool, str]:
        """Clasifica un error HTTP de Groq."""
        if status_code == 429:
            return "quota", True, "RateLimitExceeded"
        if status_code in (500, 502, 503, 504):
            return "server", True, f"HTTP{status_code}"
        if status_code == 401:
            return "auth", False, "HTTP401"
        if status_code == 403:
            return "permission", False, "HTTP403"
        if status_code == 400:
            return "invalid_argument", False, "HTTP400"
        if status_code == 404:
            return "invalid_argument", False, "HTTP404"
        if status_code is not None and 500 <= status_code < 600:
            return "server", True, f"HTTP{status_code}"
        return "unknown", False, "UnknownError"

    # ------------------------------------------------------------------
    # Interfaz AIProvider
    # ------------------------------------------------------------------

    def generate(
        self,
        contents: Any,
        config: Any,
        business_id: int | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        timeout_s: float | None = None,
        deadline_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Any | None, int, tuple[str, str] | None]:
        key = api_key or os.getenv(self._key_env)
        if not key:
            return None, 0, ("auth", "MissingAPIKey")

        model = getattr(config, "model", None) or os.getenv(self._model_env, self._default_model)

        messages = self._contents_to_messages(contents)
        tools = self._declarations_to_tools(getattr(config, "tools", None) if config else None)
        extra = self._config_to_openai(config)

        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(extra)

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        attempt = 0
        max_attempts = 2
        last_error_info: tuple[str, str] | None = None

        for attempt in range(1, max_attempts + 1):
            # Verificar deadline antes de cada intento.
            if deadline_s is not None:
                remaining = deadline_s - time.monotonic()
                if remaining <= 0:
                    return None, attempt, ("timeout", "GlobalTimeout")
            request_timeout = (
                min(timeout_s, remaining)
                if (timeout_s is not None and deadline_s is not None)
                else (timeout_s if timeout_s is not None else self._timeout_s)
            )
            start = time.monotonic()
            try:
                with httpx.Client(timeout=request_timeout) as client:
                    resp = client.post(self._chat_endpoint, json=payload, headers=headers)
                latency_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code == 200:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    message = choice.get("message", {})
                    function_calls = self._extract_function_calls(message)
                    text = self._extract_text(message)
                    usage = data.get("usage", {})

                    normalized = _GeminiCompatibleResponse(
                        text=text, function_calls=function_calls, usage=usage
                    )

                    # Log estructurado (sin exponer la API Key)
                    logger = __import__("logging").getLogger(self._log_name)
                    logger.info(
                        "groq_call request_id=%s business_id=%s latency_ms=%d attempt=%d status=success model=%s",
                        request_id or "-",
                        business_id or "-",
                        latency_ms,
                        attempt,
                        model,
                    )
                    return normalized, attempt, None

                # Error HTTP
                category, is_transient, error_type = self._classify_http_error(resp.status_code)
                last_error_info = (category, error_type)
                if not is_transient or attempt >= max_attempts:
                    return None, attempt, last_error_info
                delay = min(0.1 * (2 ** (attempt - 1)), 1.0)
                # Solo hacer backoff si queda tiempo antes del deadline.
                if deadline_s is not None:
                    remaining_after = deadline_s - time.monotonic()
                    if remaining_after <= delay:
                        return None, attempt, ("timeout", "GlobalTimeout")
                time.sleep(delay)

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - start) * 1000)
                last_error_info = ("timeout", "TimeoutError")
                if attempt >= max_attempts:
                    return None, attempt, last_error_info
                delay = min(0.1 * (2 ** (attempt - 1)), 1.0)
                if deadline_s is not None:
                    remaining_after = deadline_s - time.monotonic()
                    if remaining_after <= delay:
                        return None, attempt, ("timeout", "GlobalTimeout")
                time.sleep(delay)

            except httpx.NetworkError as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                last_error_info = ("server", exc.__class__.__name__)
                if attempt >= max_attempts:
                    return None, attempt, last_error_info
                delay = min(0.1 * (2 ** (attempt - 1)), 1.0)
                if deadline_s is not None:
                    remaining_after = deadline_s - time.monotonic()
                    if remaining_after <= delay:
                        return None, attempt, ("timeout", "GlobalTimeout")
                    time.sleep(delay)

            except Exception as exc:  # pragma: no cover - errores inesperados
                latency_ms = int((time.monotonic() - start) * 1000)
                last_error_info = ("unknown", exc.__class__.__name__)
                return None, attempt, last_error_info

        if last_error_info is not None:
            return None, attempt, last_error_info
        return None, attempt, ("unknown", "UnknownError")

    def classify_error(self, error: Exception) -> tuple[str, bool, str]:
        if isinstance(error, (TimeoutError,)):
            return "timeout", True, "TimeoutError"
        if isinstance(error, httpx.TimeoutException):
            return "timeout", True, "HttpTimeout"
        if isinstance(error, httpx.NetworkError):
            return "server", True, error.__class__.__name__
        if isinstance(error, ConnectionError):
            return "server", True, "ConnectionError"
        if isinstance(error, OSError):
            return "server", True, "OSError"
        return self._classify_http_error(getattr(error, "status_code", None))

    def is_transient_error(self, error: Exception) -> bool:
        _, is_transient, _ = self.classify_error(error)
        return is_transient

    def error_message(self, category: str) -> str:
        messages = {
            "timeout": (
                "Disculpá, la consulta tardó demasiado. Por favor, intentá nuevamente en unos momentos."
            ),
            "quota": (
                "Disculpá, el servicio de IA está temporalmente con limitaciones. "
                "Por favor, intentá nuevamente en unos minutos."
            ),
            "auth": (
                "Disculpá, hubo un problema al autenticar con el servicio de IA. "
                "Verificá las credenciales configuradas."
            ),
            "problem": ("Disculpá, hubo un problema al conectar con el servicio de IA."),
            "permission": (
                "Disculpá, el servicio de IA no tiene permisos para procesar "
                "esta solicitud. Verificá la configuración del proveedor."
            ),
            "invalid_argument": (
                "Disculpá, no pude procesar tu consulta. Por favor, intentá formularla de otra manera."
            ),
            "server": (
                "Disculpá, en este momento estoy teniendo un problema para procesar tu consulta."
            ),
        }
        return messages.get(
            category,
            "Disculpá, en este momento estoy teniendo un problema para procesar tu consulta.",
        )

    def log_call(
        self,
        request_id=None,
        business_id=None,
        latency_ms=0,
        attempt=0,
        status="unknown",
        error_type=None,
        prompt_tokens=None,
        candidate_tokens=None,
        total_tokens=None,
    ):
        logger = __import__("logging").getLogger("turnobot.ai.groq")
        logger.info(
            "groq_call request_id=%s business_id=%s latency_ms=%d attempt=%d status=%s error_type=%s prompt_tokens=%s candidate_tokens=%s total_tokens=%s",
            request_id or "-",
            business_id or "-",
            latency_ms,
            attempt,
            status,
            error_type or "-",
            prompt_tokens if prompt_tokens is not None else "-",
            candidate_tokens if candidate_tokens is not None else "-",
            total_tokens if total_tokens is not None else "-",
        )


class OpenRouterProvider(GroqProvider):
    """Preset OpenAI-compatible para OpenRouter."""

    _provider_key = "openrouter"
    _key_env = "OPENROUTER_API_KEY"
    _model_env = "OPENROUTER_MODEL"
    _default_model = "openai/gpt-4o-mini"
    _timeout_s = float(os.getenv("OPENROUTER_TIMEOUT_S", "8"))
    _chat_endpoint = (
        f"{os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')}/chat/completions"
    )
    _log_name = "turnobot.ai.openrouter"


class CerebrasProvider(GroqProvider):
    """Preset OpenAI-compatible para Cerebras."""

    _provider_key = "cerebras"
    _key_env = "CEREBRAS_API_KEY"
    _model_env = "CEREBRAS_MODEL"
    _default_model = "llama3.1-8b"
    _timeout_s = float(os.getenv("CEREBRAS_TIMEOUT_S", "8"))
    _chat_endpoint = (
        f"{os.getenv('CEREBRAS_BASE_URL', 'https://api.cerebras.ai/v1')}/chat/completions"
    )
    _log_name = "turnobot.ai.cerebras"
