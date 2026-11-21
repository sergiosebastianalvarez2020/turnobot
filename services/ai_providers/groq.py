"""
services/ai_providers/groq.py — Preset Groq del adaptador OpenAI-compatible.

La lógica compartida vive en `services.ai_providers.openai_compatible`;
este módulo mantiene el punto de importación histórico y el nombre del
constante de timeout usado por las pruebas existentes.
"""

from __future__ import annotations

from services.ai_providers.openai_compatible import (
    CerebrasProvider,
    GroqProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
)

_GROQ_TIMEOUT_S = 8.0

__all__ = ["CerebrasProvider", "GroqProvider", "OpenRouterProvider", "OpenAICompatibleProvider"]
