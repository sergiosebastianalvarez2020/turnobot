"""Proveedores de inteligencia artificial (abstraccion multi-proveedor).

Este paquete contiene la interfaz comun (AIProvider) y los adaptadores
concretos para cada proveedor de IA integrado en TurnoBot.

En la Etapa 1 solo existe el adaptador de Google Gemini. Los adaptadores
de Groq, OpenRouter y Cerebras se agregaran en etapas posteriores sin
necesidad de modificar ask_ai() ni la logica central de servicios/ai.py.
"""

from services.ai_providers.base import AIProvider

__all__ = ["AIProvider"]
