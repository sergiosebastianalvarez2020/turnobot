"""
services/ai_providers/openai_compatible.py — Adaptador reutilizable para APIs compatibles con OpenAI.

Groq, OpenRouter y Cerebras exponen /chat/completions con el mismo contrato.
Este módulo concentra la conversión de formatos, reintentos, deadlines y
clasificación de errores; cada proveedor solo aporta su nombre, base URL,
variable de credencial y modelo por defecto.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx

from services.ai_providers.base import AIProvider, ProviderError, classify_status_code


class _MalformedResponse(Exception):
    def __init__(self, code: str = "MalformedResponse"):
        self.code = code
        super().__init__(code)


class _GeminiCompatibleResponse:
    """Envuelve una respuesta OpenAI-like para que parezca una respuesta Gemini."""

    def __init__(self, text: str | None, function_calls: list[dict], usage: dict | None):
        self._text = text
        self._function_calls = function_calls
        self._usage = usage or {}

    @property
    def text(self) -> str:
        return self._text or ""

    @property
    def candidates(self) -> list:
        parts: list = []
        if self._text:
            parts.append(_TextPart(self._text))
        for fc in self._function_calls:
            parts.append(_FunctionCallPart(fc))
        return [type("Candidate", (), {"content": type("Content", (), {"parts": parts})})()]

    @property
    def usage_metadata(self) -> dict:
        return self._usage


class _FunctionCallPart:
    def __init__(self, fc: dict):
        self.function_call = _FunctionCall(fc)


class _FunctionCall:
    def __init__(self, fc: dict):
        self.id = fc.get("id", "")
        self.name = fc.get("name", "")
        self.args = fc.get("arguments", {})


class _TextPart:
    def __init__(self, text: str):
        self.text = text


class OpenAICompatibleProvider(AIProvider):
    """Adaptador parametrizable para proveedores con API OpenAI-compatible."""

    provider_key = "openai_compatible"
    key_env = "OPENAI_API_KEY"
    base_url_env = "OPENAI_BASE_URL"
    default_base_url = ""
    model_env = "OPENAI_MODEL"
    default_model = ""
    timeout_env = "OPENAI_TIMEOUT_S"
    default_timeout_s = 8.0
    log_name = "turnobot.ai.openai_compatible"

    def provider_name(self) -> str:
        return self.provider_key

    def is_configured(self) -> bool:
        return bool(os.getenv(self.key_env))

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        parts = getattr(value, "parts", None)
        if parts:
            chunks = []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                else:
                    text = getattr(part, "text", None)
                if text:
                    chunks.append(str(text))
            if chunks:
                return "\n".join(chunks)
        return str(value)

    @staticmethod
    def _field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _contents_to_messages(contents: Any) -> list[dict]:
        """Convierte contenidos Gemini-like a mensajes OpenAI-like."""
        messages: list[dict] = []
        tool_call_ids: dict[str, str] = {}
        for content in contents or []:
            role = OpenAICompatibleProvider._field(content, "role", "user") or "user"
            if role == "model":
                role = "assistant"
            parts = OpenAICompatibleProvider._field(content, "parts", []) or []
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            tool_messages: list[dict] = []
            for part in parts:
                fc = OpenAICompatibleProvider._field(part, "function_call")
                if fc is not None:
                    call_id = (
                        OpenAICompatibleProvider._field(fc, "id", None)
                        or f"call_{uuid.uuid4().hex[:12]}"
                    )
                    name = OpenAICompatibleProvider._field(fc, "name", "") or ""
                    args = OpenAICompatibleProvider._field(fc, "args", {}) or {}
                    tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    )
                    tool_call_ids[name] = call_id
                    continue

                fr = OpenAICompatibleProvider._field(part, "function_response")
                if fr is not None:
                    name = OpenAICompatibleProvider._field(fr, "name", "") or ""
                    response = OpenAICompatibleProvider._field(fr, "response", {})
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_ids.get(
                                name, f"call_{uuid.uuid4().hex[:12]}"
                            ),
                            "name": name,
                            "content": json.dumps(response, default=str),
                        }
                    )
                    continue

                text = OpenAICompatibleProvider._field(part, "text")
                if text:
                    text_parts.append(str(text))

            message: dict[str, Any] = {"role": role}
            if text_parts:
                message["content"] = "\n".join(text_parts)
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            messages.extend(tool_messages)
        return messages

    @staticmethod
    def _declarations_to_tools(tools: Any) -> list[dict] | None:
        """Convierte declaraciones de herramientas Gemini a formato OpenAI."""
        if not tools:
            return None
        openai_tools = []
        for tool in tools:
            fds = OpenAICompatibleProvider._field(tool, "function_declarations", None) or []
            for fd in fds:
                openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": OpenAICompatibleProvider._field(fd, "name", ""),
                            "description": OpenAICompatibleProvider._field(fd, "description", "")
                            or "",
                            "parameters": OpenAICompatibleProvider._field(
                                fd,
                                "parameters_json_schema",
                                {"type": "object", "properties": {}, "required": []},
                            )
                            or {"type": "object", "properties": {}, "required": []},
                        },
                    }
                )
        return openai_tools or None

    @staticmethod
    def _config_to_openai(config: Any) -> dict:
        """Convierte parámetros de generación compatibles a parámetros OpenAI."""
        result: dict[str, Any] = {}
        if config is None:
            return result
        temperature = getattr(config, "temperature", None)
        if temperature is not None:
            result["temperature"] = temperature
        max_tokens = getattr(config, "max_output_tokens", None)
        if max_tokens is not None:
            result["max_tokens"] = max_tokens
        return result

    @staticmethod
    def _extract_function_calls(message: dict) -> list[dict]:
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(
                {"id": tc.get("id", "call_0"), "name": fn.get("name", ""), "arguments": args}
            )
        return calls

    @staticmethod
    def _extract_text(message: dict) -> str | None:
        return message.get("content")

    @staticmethod
    def _extract_structured_code(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        for source in (error, payload):
            if not isinstance(source, dict):
                continue
            for key in ("code", "type", "status"):
                value = source.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        return None

    @staticmethod
    def _classify_http_provider_error(
        status_code: int | None, payload: Any = None
    ) -> ProviderError:
        structured = OpenAICompatibleProvider._extract_structured_code(payload)
        return classify_status_code(status_code, structured)

    @staticmethod
    def _classify_http_error(status_code: int | None, payload: Any = None) -> tuple[str, bool, str]:
        error = OpenAICompatibleProvider._classify_http_provider_error(status_code, payload)
        return error.category, error.retry_same_provider, error.code

    @staticmethod
    def _parse_success_payload(data: Any) -> _GeminiCompatibleResponse:
        if not isinstance(data, dict):
            raise _MalformedResponse("MalformedResponse")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _MalformedResponse("EmptyChoices")
        first = choices[0]
        if not isinstance(first, dict):
            raise _MalformedResponse("MalformedChoice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise _MalformedResponse("MissingMessage")
        text = message.get("content")
        if text is not None and not isinstance(text, str):
            raise _MalformedResponse("MalformedContent")
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and (
            not isinstance(tool_calls, list)
            or any(
                not isinstance(call, dict) or not isinstance(call.get("function"), dict)
                for call in tool_calls
            )
        ):
            raise _MalformedResponse("MalformedToolCalls")
        if (text is None or not text.strip()) and not tool_calls:
            raise _MalformedResponse("EmptyResponse")
        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise _MalformedResponse("MalformedUsage")
        return _GeminiCompatibleResponse(
            text=text,
            function_calls=OpenAICompatibleProvider._extract_function_calls(message),
            usage=usage,
        )

    @classmethod
    def _configured_timeout_s(cls) -> float:
        try:
            return float(os.getenv(cls.timeout_env, str(cls.default_timeout_s)))
        except (TypeError, ValueError):
            return cls.default_timeout_s

    @staticmethod
    def _wait_before_retry(attempt: int, deadline_s: float | None) -> bool:
        delay = min(0.1 * (2 ** (attempt - 1)), 1.0)
        if deadline_s is not None and deadline_s - time.monotonic() <= delay:
            return False
        time.sleep(delay)
        return True

    @property
    def _chat_endpoint(self) -> str:
        base_url = os.getenv(self.base_url_env, self.default_base_url).rstrip("/")
        return f"{base_url}/chat/completions"

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
        model: str | None = None,
    ) -> tuple[Any | None, int, ProviderError | None]:
        key = api_key or os.getenv(self.key_env)
        if not key:
            return (
                None,
                0,
                ProviderError(
                    "auth", "MissingAPIKey", retry_same_provider=False, fallback_next_provider=True
                ),
            )

        resolved_model = (
            model
            or getattr(config, "model", None)
            or os.getenv(self.model_env)
            or self.default_model
        )

        messages = self._contents_to_messages(contents)
        system_instruction = getattr(config, "system_instruction", None) if config else None
        if system_instruction:
            messages.insert(0, {"role": "system", "content": self._stringify(system_instruction)})
        tools = self._declarations_to_tools(getattr(config, "tools", None) if config else None)

        payload: dict[str, Any] = {"model": resolved_model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(self._config_to_openai(config))

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        max_attempts = 2
        http_attempts = 0
        last_error: ProviderError | None = None

        for attempt in range(1, max_attempts + 1):
            configured_timeout = self._configured_timeout_s()
            request_timeout = (
                min(timeout_s, configured_timeout) if timeout_s is not None else configured_timeout
            )
            if deadline_s is not None:
                remaining = deadline_s - time.monotonic()
                if remaining <= 0:
                    error = last_error or ProviderError(
                        "timeout",
                        "GlobalTimeout",
                        retry_same_provider=True,
                        fallback_next_provider=True,
                    )
                    return None, http_attempts, error
                request_timeout = min(request_timeout, remaining)
                if request_timeout <= 0:
                    return (
                        None,
                        http_attempts,
                        ProviderError(
                            "timeout",
                            "GlobalTimeout",
                            retry_same_provider=True,
                            fallback_next_provider=True,
                        ),
                    )

            start = time.monotonic()
            try:
                with httpx.Client(timeout=request_timeout) as client:
                    http_attempts += 1
                    resp = client.post(self._chat_endpoint, json=payload, headers=headers)
                latency_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except (ValueError, TypeError):
                        last_error = ProviderError(
                            "malformed_response",
                            "MalformedJSON",
                            retry_same_provider=True,
                            fallback_next_provider=True,
                        )
                    else:
                        try:
                            normalized = self._parse_success_payload(data)
                        except _MalformedResponse as malformed:
                            last_error = ProviderError(
                                "malformed_response",
                                malformed.code,
                                retry_same_provider=True,
                                fallback_next_provider=True,
                            )
                        else:
                            self.log_call(
                                request_id=request_id,
                                business_id=business_id,
                                latency_ms=latency_ms,
                                attempt=attempt,
                                status="success",
                            )
                            return normalized, http_attempts, None
                else:
                    try:
                        error_payload = resp.json()
                    except (ValueError, TypeError):
                        error_payload = None
                    last_error = self._classify_http_provider_error(resp.status_code, error_payload)

                self.log_call(
                    request_id=request_id,
                    business_id=business_id,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    status="error",
                    error_type=last_error.code,
                )
                if last_error.retry_same_provider and attempt < max_attempts:
                    if not self._wait_before_retry(attempt, deadline_s):
                        return (
                            None,
                            http_attempts,
                            ProviderError(
                                "timeout",
                                "GlobalTimeout",
                                retry_same_provider=True,
                                fallback_next_provider=True,
                            ),
                        )
                    continue
                return None, http_attempts, last_error

            except httpx.TimeoutException:
                last_error = ProviderError(
                    "timeout", "TimeoutError", retry_same_provider=True, fallback_next_provider=True
                )
            except (httpx.ConnectError, httpx.NetworkError) as exc:
                last_error = ProviderError(
                    "server",
                    exc.__class__.__name__,
                    retry_same_provider=True,
                    fallback_next_provider=True,
                )
            self.log_call(
                request_id=request_id,
                business_id=business_id,
                latency_ms=int((time.monotonic() - start) * 1000),
                attempt=attempt,
                status="error",
                error_type=last_error.code,
            )
            if last_error.retry_same_provider and attempt < max_attempts:
                if not self._wait_before_retry(attempt, deadline_s):
                    return (
                        None,
                        http_attempts,
                        ProviderError(
                            "timeout",
                            "GlobalTimeout",
                            retry_same_provider=True,
                            fallback_next_provider=True,
                        ),
                    )
                continue
            return None, http_attempts, last_error

        return None, http_attempts, last_error or ProviderError("unknown", "UnknownError")

    def classify_error(self, error: Exception) -> tuple[str, bool, str]:
        if isinstance(error, TimeoutError):
            return "timeout", True, "TimeoutError"
        if isinstance(error, httpx.TimeoutException):
            return "timeout", True, "HttpTimeout"
        if isinstance(error, (httpx.ConnectError, httpx.NetworkError, ConnectionError, OSError)):
            return "server", True, error.__class__.__name__
        return self._classify_http_error(getattr(error, "status_code", None))

    def is_transient_error(self, error: Exception) -> bool:
        _, is_transient, _ = self.classify_error(error)
        return is_transient

    def error_message(self, category: str) -> str:
        messages = {
            "timeout": "Disculpá, la consulta tardó demasiado. Por favor, intentá nuevamente en unos momentos.",
            "quota": "Disculpá, el servicio de IA está temporalmente con limitaciones. Por favor, intentá nuevamente en unos minutos.",
            "auth": "Disculpá, hubo un problema al autenticar con el servicio de IA. Verificá las credenciales configuradas.",
            "permission": "Disculpá, el servicio de IA no tiene permisos para procesar esta solicitud. Verificá la configuración del proveedor.",
            "policy_denied": "Disculpá, el proveedor de IA denegó esta solicitud por su política de acceso.",
            "invalid_argument": "Disculpá, no pude procesar tu consulta. Por favor, intentá formularla de otra manera.",
            "malformed_response": "Disculpá, el proveedor de IA devolvió una respuesta inesperada.",
            "server": "Disculpá, en este momento estoy teniendo un problema para procesar tu consulta.",
        }
        return messages.get(
            category,
            "Disculpá, en este momento estamos teniendo un problema para procesar tu consulta.",
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
        import logging

        logging.getLogger(self.log_name).info(
            "%s_call request_id=%s business_id=%s latency_ms=%d attempt=%d status=%s error_type=%s",
            self.provider_key,
            request_id or "-",
            business_id or "-",
            latency_ms,
            attempt,
            status,
            error_type or "-",
        )


class GroqProvider(OpenAICompatibleProvider):
    provider_key = "groq"
    key_env = "GROQ_API_KEY"
    base_url_env = "GROQ_BASE_URL"
    default_base_url = "https://api.groq.com/openai/v1"
    model_env = "GROQ_MODEL"
    default_model = "llama-3.1-8b-instant"
    timeout_env = "GROQ_TIMEOUT_S"
    default_timeout_s = 8.0
    log_name = "turnobot.ai.groq"


class OpenRouterProvider(OpenAICompatibleProvider):
    provider_key = "openrouter"
    key_env = "OPENROUTER_API_KEY"
    base_url_env = "OPENROUTER_BASE_URL"
    default_base_url = "https://openrouter.ai/api/v1"
    model_env = "OPENROUTER_MODEL"
    default_model = "openai/gpt-4o-mini"
    timeout_env = "OPENROUTER_TIMEOUT_S"
    default_timeout_s = 8.0
    log_name = "turnobot.ai.openrouter"


class CerebrasProvider(OpenAICompatibleProvider):
    provider_key = "cerebras"
    key_env = "CEREBRAS_API_KEY"
    base_url_env = "CEREBRAS_BASE_URL"
    default_base_url = "https://api.cerebras.ai/v1"
    model_env = "CEREBRAS_MODEL"
    default_model = "llama3.1-8b"
    timeout_env = "CEREBRAS_TIMEOUT_S"
    default_timeout_s = 8.0
    log_name = "turnobot.ai.cerebras"
