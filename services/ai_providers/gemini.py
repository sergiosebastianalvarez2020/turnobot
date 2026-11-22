"""
services/ai_providers/gemini.py — Adaptador de Google Gemini.

Encapsula la integracion actual de Google Gemini (SDK google-genai==2.18.1)
detras de la interfaz AIProvider.

Toda la logica de cliente, reintentos, clasificacion de errores y logging
se conserva en services/ai.py. Este adaptador implementa la interfaz
AIProvider delegando en esas funciones, lo que permite que ask_ai() use
el proveedor sin depender directamente del SDK de Gemini.

En etapas futuras se agregaran adaptadores para Groq, OpenRouter y
Cerebras siguiendo el mismo patron, sin modificar ask_ai().
"""

from __future__ import annotations

import os
from typing import Any

from services.ai_providers.base import AIProvider, ProviderError


class GeminiProvider(AIProvider):
    """Adaptador de Google Gemini bajo la interfaz AIProvider."""

    def provider_name(self) -> str:
        return "gemini"

    def is_configured(self) -> bool:
        return bool(os.getenv("GEMINI_API_KEY"))

    # ------------------------------------------------------------------
    # Delegacion hacia services/ai.py (logica existente preservada)
    # ------------------------------------------------------------------

    def get_client(self):
        from services.ai import get_gemini_client

        return get_gemini_client()

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
        from services.ai import _call_gemini_with_retry

        return _call_gemini_with_retry(
            contents,
            config,
            request_id=request_id,
            business_id=business_id,
            api_key=api_key,
            timeout_s=timeout_s,
            deadline_s=deadline_s,
            model=model,
        )

    def classify_error(self, error: Exception) -> tuple[str, bool, str]:
        from services.ai import _classify_gemini_error

        return _classify_gemini_error(error)

    def is_transient_error(self, error: Exception) -> bool:
        from services.ai import _is_transient_error

        return _is_transient_error(error)

    def error_message(self, category: str) -> str:
        from services.ai import _gemini_error_message

        return _gemini_error_message(category)

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
        from services.ai import _log_gemini_call

        _log_gemini_call(
            request_id=request_id,
            business_id=business_id,
            latency_ms=latency_ms,
            attempt=attempt,
            status=status,
            error_type=error_type,
            prompt_tokens=prompt_tokens,
            candidate_tokens=candidate_tokens,
            total_tokens=total_tokens,
        )
