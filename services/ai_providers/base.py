"""
services/ai_providers/base.py — Interfaz común para proveedores de IA.

Define el contrato que debe cumplir cualquier proveedor de inteligencia
artificial integrado en TurnoBot. La interfaz es intencionalmente mínima:
solo cubre lo que ask_ai() necesita para orquestar llamadas sin conocer
al proveedor concreto.

Cada proveedor concreto (Gemini, Groq, OpenRouter, Cerebras) implementa
esta interfaz encapsulando su propio SDK, formato de API, clasificacion
de errores y politica de reintentos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AIProvider(ABC):
    """Interfaz base para proveedores de IA."""

    @abstractmethod
    def provider_name(self) -> str:
        """Nombre identificador del proveedor (ej. 'gemini', 'groq')."""

    @abstractmethod
    def is_configured(self) -> bool:
        """True si el proveedor tiene credenciales y puede usarse."""

    @abstractmethod
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
        """
        Ejecuta una generacion de contenido a través del proveedor.

        Args:
            contents: Historial de mensajes en formato propio del proveedor.
            config: Configuracion de generacion en formato propio del proveedor.
            business_id: ID del negocio (para logging y trazabilidad).
            request_id: ID de la solicitud (para logging y trazabilidad).
            api_key: API Key opcional para sobrescribir la configurada por defecto.
            timeout_s: Tiempo máximo en segundos para la llamada. Si se provee,
                       el proveedor debe respetarlo como límite superior.
            deadline_s: Momento absoluto (time.monotonic()) en el que toda la
                       operación del router debe terminar. Si se provee, el
                       proveedor debe respetarlo como límite superior y no
                       iniciar reintentos ni esperas que lo excedan.

        Returns:
            Tupla (respuesta_o_None, intentos, informacion_de_error_o_None).
            informacion_de_error es (categoria, tipo_error) o None si tuvo exito.
        """

    @abstractmethod
    def classify_error(self, error: Exception) -> tuple[str, bool, str]:
        """
        Clasifica un error del proveedor.

        Returns:
            Tupla (categoria, es_transitorio, tipo_error).
            categoria: 'timeout', 'quota', 'auth', 'invalid_argument',
                       'server', 'unknown'.
            es_transitorio: True si el error puede reintentarse.
            tipo_error: string corto para logging.
        """

    @abstractmethod
    def is_transient_error(self, error: Exception) -> bool:
        """True si el error es transitorio y merece reintento."""

    @abstractmethod
    def error_message(self, category: str) -> str:
        """Mensaje amigable para el usuario segun la categoria de error."""
