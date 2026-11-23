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


def _global_timeout_error() -> ProviderError:
    """Error de timeout global (deadline agotado o timeout no positivo)."""
    return ProviderError(
        "timeout", "GlobalTimeout", retry_same_provider=True, fallback_next_provider=True
    )


def _deadline_timeout_error(last_error: ProviderError | None) -> ProviderError:
    """Error de deadline global: conserva el último error si existe."""
    return last_error or _global_timeout_error()


def _exhausted_error(last_error: ProviderError | None) -> ProviderError:
    """Error final cuando se agotan los reintentos del proveedor."""
    return last_error or ProviderError("unknown", "UnknownError")


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
    def _extract_text_from_part(part: dict) -> str | None:
        """Extrae el texto de una parte de contenido."""
        return OpenAICompatibleProvider._field(part, "text")

    @staticmethod
    def _extract_tool_calls_from_parts(
        parts: list[dict], call_ids: dict[str, str]
    ) -> tuple[list[dict], list[dict]]:
        """Extrae llamadas a herramientas y mensajes de tool_response de las parts."""
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
                call_ids[name] = call_id
                continue

            fr = OpenAICompatibleProvider._field(part, "function_response")
            if fr is not None:
                name = OpenAICompatibleProvider._field(fr, "name", "") or ""
                response = OpenAICompatibleProvider._field(fr, "response", {})
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_ids.get(name, f"call_{uuid.uuid4().hex[:12]}"),
                        "name": name,
                        "content": json.dumps(response, default=str),
                    }
                )
        return tool_calls, tool_messages

    @staticmethod
    def _build_message(role: str, text_parts: list[str], tool_calls: list[dict]) -> dict:
        """Construye un mensaje dict con role, content y tool_calls."""
        message: dict[str, Any] = {"role": role}
        if text_parts:
            message["content"] = "\n".join(text_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    @staticmethod
    def _extract_text_parts(parts: list[dict]) -> list[str]:
        """Extrae los textos de las parts ignorando function_call/function_response."""
        text_parts: list[str] = []
        for part in parts:
            fc = OpenAICompatibleProvider._field(part, "function_call")
            if fc is not None:
                continue
            fr = OpenAICompatibleProvider._field(part, "function_response")
            if fr is not None:
                continue
            text = OpenAICompatibleProvider._field(part, "text")
            if text:
                text_parts.append(str(text))
        return text_parts

    @staticmethod
    def _contents_to_messages(contents: Any) -> list[dict]:
        """Convierte contenidos Gemini-like a mensajes OpenAI-like."""
        messages: list[dict] = []
        call_ids: dict[str, str] = {}
        for content in contents or []:
            role = OpenAICompatibleProvider._field(content, "role", "user") or "user"
            if role == "model":
                role = "assistant"
            parts = OpenAICompatibleProvider._field(content, "parts", []) or []

            text_parts = OpenAICompatibleProvider._extract_text_parts(parts)
            tool_calls, tool_messages = OpenAICompatibleProvider._extract_tool_calls_from_parts(
                parts, call_ids
            )

            message = OpenAICompatibleProvider._build_message(role, text_parts, tool_calls)
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
    def _extract_message(data: dict) -> dict:
        """Extrae y valida el primer message de la respuesta.

        Raises:
            _MalformedResponse: si choices/message tienen estructura inválida.
        """
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _MalformedResponse("EmptyChoices")
        first = choices[0]
        if not isinstance(first, dict):
            raise _MalformedResponse("MalformedChoice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise _MalformedResponse("MissingMessage")
        return message

    @staticmethod
    def _validate_message_content(text, tool_calls) -> None:
        """Valida el contenido del message.

        Raises:
            _MalformedResponse: si content/tool_calls son inválidos o vacíos.
        """
        if text is not None and not isinstance(text, str):
            raise _MalformedResponse("MalformedContent")
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

    @staticmethod
    def _parse_success_payload(data: Any) -> _GeminiCompatibleResponse:
        if not isinstance(data, dict):
            raise _MalformedResponse("MalformedResponse")
        message = OpenAICompatibleProvider._extract_message(data)
        text = message.get("content")
        tool_calls = message.get("tool_calls")
        OpenAICompatibleProvider._validate_message_content(text, tool_calls)
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

    def _resolve_credentials_and_model(
        self, api_key: str | None, config: Any, model: str | None
    ) -> tuple[str | None, str]:
        """Resuelve la API key y el modelo a utilizar.

        Returns:
            Tupla de (key_resuelta_o_None, model_resuelto).
        """
        key = api_key or os.getenv(self.key_env)

        resolved_model = (
            model
            or getattr(config, "model", None)
            or os.getenv(self.model_env)
            or self.default_model
        )
        return key, resolved_model

    @staticmethod
    def _convert_messages_and_tools(
        contents: Any, config: Any
    ) -> tuple[list[dict], list[dict] | None]:
        """Convierte contenidos Gemini a formato OpenAI y herramientas.

        Returns:
            Tupla de (messages, tools).
        """
        messages = OpenAICompatibleProvider._contents_to_messages(contents)
        system_instruction = getattr(config, "system_instruction", None) if config else None
        if system_instruction:
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": OpenAICompatibleProvider._stringify(system_instruction),
                },
            )

        tools = OpenAICompatibleProvider._declarations_to_tools(
            getattr(config, "tools", None) if config else None
        )
        return messages, tools

    @staticmethod
    def _construct_headers(api_key: str) -> dict[str, str]:
        """Construye los headers de autorización."""
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _base_request_timeout(
        requested_timeout_s: float | None, configured_timeout_s: float
    ) -> float:
        """Timeout base de la petición respetando lo pedido y lo configurado."""
        if requested_timeout_s is not None:
            return min(requested_timeout_s, configured_timeout_s)
        return configured_timeout_s

    @staticmethod
    def _handle_successful_response(
        normalized: _GeminiCompatibleResponse,
        log_call,
        request_id,
        business_id,
        latency_ms,
        attempt,
        http_attempts,
    ) -> tuple[_GeminiCompatibleResponse, int, None]:
        """Registra el éxito con métricas de tokens y devuelve la tupla de retorno.

        Returns:
            Tupla de (normalized_response, http_attempts, None).
        """
        usage = normalized.usage_metadata or {}
        log_call(
            request_id=request_id,
            business_id=business_id,
            latency_ms=latency_ms,
            attempt=attempt,
            status="success",
            prompt_tokens=usage.get("prompt_tokens"),
            candidate_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        return normalized, http_attempts, None

    def _parse_http_response(
        self, resp
    ) -> tuple[_GeminiCompatibleResponse | None, ProviderError | None]:
        """Parsea una respuesta HTTP 200.

        Returns:
            Tupla de (respuesta_normalizada, None) o (None, error).
        """
        try:
            data = resp.json()
        except (ValueError, TypeError):
            return None, ProviderError(
                "malformed_response",
                "MalformedJSON",
                retry_same_provider=True,
                fallback_next_provider=True,
            )
        try:
            return self._parse_success_payload(data), None
        except _MalformedResponse as malformed:
            return None, ProviderError(
                "malformed_response",
                malformed.code,
                retry_same_provider=True,
                fallback_next_provider=True,
            )

    def _classify_http_response_error(self, resp) -> ProviderError:
        """Clasifica una respuesta HTTP con status distinto de 200."""
        try:
            error_payload = resp.json()
        except (ValueError, TypeError):
            error_payload = None
        return self._classify_http_provider_error(resp.status_code, error_payload)

    def _error_outcome(
        self,
        error: ProviderError,
        latency_ms: int,
        attempt: int,
        max_attempts: int,
        request_id,
        business_id,
        http_attempts: int,
        deadline_s: float | None,
    ):
        """Registra el error de un intento y decide reintentar o retornar.

        Returns:
            Tupla (retorno_terminal_o_None, error, http_attempts).
            ``retorno`` None => reintentar (backoff ya aplicado).
        """
        self.log_call(
            request_id=request_id,
            business_id=business_id,
            latency_ms=latency_ms,
            attempt=attempt,
            status="error",
            error_type=error.code,
        )
        if error.retry_same_provider and attempt < max_attempts:
            if not self._wait_before_retry(attempt, deadline_s):
                return (None, http_attempts, _global_timeout_error()), error, http_attempts
            return None, error, http_attempts
        return (None, http_attempts, error), error, http_attempts

    def _run_http_attempt(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        request_timeout: float,
        attempt: int,
        max_attempts: int,
        request_id,
        business_id,
        deadline_s: float | None,
        http_attempts: int,
    ):
        """Ejecuta un intento HTTP completo y clasifica su resultado.

        Returns:
            Tupla (retorno_terminal_o_None, last_error, http_attempts).
            ``retorno`` None => reintentar (backoff ya aplicado).
        """
        start = time.monotonic()
        try:
            with httpx.Client(timeout=request_timeout) as client:
                http_attempts += 1
                resp = client.post(self._chat_endpoint, json=payload, headers=headers)
            latency_ms = int((time.monotonic() - start) * 1000)

            if resp.status_code == 200:
                normalized, parse_error = self._parse_http_response(resp)
                if normalized is not None:
                    outcome = self._handle_successful_response(
                        normalized,
                        self.log_call,
                        request_id,
                        business_id,
                        latency_ms,
                        attempt,
                        http_attempts,
                    )
                    return outcome, None, http_attempts
                last_error = parse_error
            else:
                last_error = self._classify_http_response_error(resp)

            if last_error is None:
                last_error = ProviderError(
                    "malformed_response",
                    "UnknownResponseError",
                    retry_same_provider=True,
                    fallback_next_provider=True,
                )

            return self._error_outcome(
                last_error,
                latency_ms,
                attempt,
                max_attempts,
                request_id,
                business_id,
                http_attempts,
                deadline_s,
            )
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
        latency_ms = int((time.monotonic() - start) * 1000)
        return self._error_outcome(
            last_error,
            latency_ms,
            attempt,
            max_attempts,
            request_id,
            business_id,
            http_attempts,
            deadline_s,
        )

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
        """Llama al proveedor de IA y devuelve respuesta, intentos y error."""

        # --- 1. Resolución de credenciales y modelo ---
        key, resolved_model = self._resolve_credentials_and_model(api_key, config, model)
        if not key:
            return (
                None,
                0,
                ProviderError(
                    "auth", "MissingAPIKey", retry_same_provider=False, fallback_next_provider=True
                ),
            )

        # --- 2. Conversión de mensajes, payload y headers ---
        messages, tools = self._convert_messages_and_tools(contents, config)
        payload: dict[str, Any] = {"model": resolved_model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(self._config_to_openai(config))
        headers = self._construct_headers(key)

        # --- 3. Preparación de reintentos ---
        max_attempts = 2
        http_attempts = 0
        last_error: ProviderError | None = None

        # --- 4. Bucle de reintentos ---
        for attempt in range(1, max_attempts + 1):
            configured_timeout = self._configured_timeout_s()
            request_timeout = self._base_request_timeout(timeout_s, configured_timeout)
            if deadline_s is not None:
                remaining = deadline_s - time.monotonic()
                if remaining <= 0:
                    return None, http_attempts, _deadline_timeout_error(last_error)
                request_timeout = min(request_timeout, remaining)
            if request_timeout <= 0:
                return None, http_attempts, _global_timeout_error()

            outcome, last_error, http_attempts = self._run_http_attempt(
                payload,
                headers,
                request_timeout,
                attempt,
                max_attempts,
                request_id,
                business_id,
                deadline_s,
                http_attempts,
            )
            if outcome is not None:
                return outcome

        # --- 5. Sin reintentos restantes ---
        return None, http_attempts, _exhausted_error(last_error)

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
            "%s_call request_id=%s business_id=%s latency_ms=%d attempt=%d status=%s "
            "error_type=%s prompt_tokens=%s candidate_tokens=%s total_tokens=%s",
            self.provider_key,
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
