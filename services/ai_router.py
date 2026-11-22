"""
services/ai_router.py — Motor inteligente de fallback multi-proveedor.

Orquesta llamadas a proveedores de IA respetando la configuración global
almacenada en `platform_ai_providers` (prioridad y estado activo).

Responsabilidades:
1. Consultar los proveedores activos desde la tabla de configuración.
2. Ordenarlos por prioridad ascendente.
3. Resolver credenciales de forma segura (desencriptado interno).
4. Seleccionar el proveedor correspondiente.
5. Ejecutar la generación.
6. Clasificar los errores.
7. Cambiar al siguiente proveedor cuando corresponda.
8. Registrar el resultado (proveedor que respondió, motivo del fallback).
9. Responder con un mensaje de contingencia si todos fallan.

El router no conoce detalles internos de cada SDK; trabaja mediante la
interfaz AIProvider.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from services.ai_config import get_ai_provider_by_name, get_decrypted_api_key, list_ai_providers
from services.ai_encryption import is_configured as encryption_is_configured
from services.ai_providers.base import AIProvider, ProviderError, classify_provider_error
from services.ai_providers.gemini import GeminiProvider
from services.ai_providers.groq import CerebrasProvider, GroqProvider, OpenRouterProvider

logger = logging.getLogger("turnobot.ai.router")

# Tiempo máximo global de ejecución en segundos (backoff acotado incluido).
# Presupuesto total: 20s. Se reserva tiempo suficiente para que cada
# proveedor activo tenga una oportunidad real de responder.
_MAX_GLOBAL_TIMEOUT_S = 20

# Mensaje de contingencia cuando todos los proveedores fallan.
_CONTINGENCY_MESSAGE = (
    "En este momento estamos teniendo dificultades para procesar tu consulta. "
    "Podés intentarlo nuevamente en unos minutos o solicitar atención de una persona."
)


def _resolve_provider_credentials(
    provider_name: str, provider_config: dict[str, Any] | None
) -> str | None:
    """
    Resuelve la API Key para un proveedor.

    Reglas:
    - Gemini con registro activo en la tabla → clave desencriptada de la tabla.
    - Gemini sin registro en la tabla → GEMINI_API_KEY (modo legacy).
    - Cualquier otro proveedor → clave desencriptada de la tabla.
    - Si AI_ENCRYPTION_KEY no está configurada y se necesita desencriptar,
      se retorna None (no se pueden usar credenciales cifradas).
    """
    if provider_name == "gemini":
        if provider_config is not None:
            # Registro en la tabla: usar credencial cifrada.
            if not encryption_is_configured():
                logger.error(
                    "AI_ENCRYPTION_KEY no está configurada; no se pueden usar credenciales de Gemini desde la tabla."
                )
                return None
            return get_decrypted_api_key(provider_config["id"])
        # Modo legacy: variable de entorno.
        return os.getenv("GEMINI_API_KEY")

    # Otros proveedores requieren registro en la tabla con credencial cifrada.
    if provider_config is None:
        return None
    if not encryption_is_configured():
        logger.error(
            "AI_ENCRYPTION_KEY no está configurada; no se pueden usar credenciales de %s desde la tabla.",
            provider_name,
        )
        return None
    return get_decrypted_api_key(provider_config["id"])


class AIProviderRouter:
    """Router de proveedores de IA con fallback automático."""

    _PROVIDER_CLASSES: dict[str, type[AIProvider]] = {
        "gemini": GeminiProvider,
        "groq": GroqProvider,
        "openrouter": OpenRouterProvider,
        "cerebras": CerebrasProvider,
    }

    def __init__(self) -> None:
        self._providers_cache: list[dict[str, Any]] | None = None
        self._all_providers_cache: list[dict[str, Any]] | None = None

    def _instantiate_provider(self, provider_name: str) -> AIProvider | None:
        """Crea una instancia del proveedor; la credencial se pasa a ``generate``."""
        cls = self._PROVIDER_CLASSES.get(provider_name)
        if cls is None:
            logger.error("Proveedor no soportado: %s", provider_name)
            return None
        try:
            return cls()
        except Exception:  # pragma: no cover - errores de inicialización
            logger.exception("Error inicializando proveedor %s", provider_name)
            return None

    def _get_active_providers(self) -> list[dict[str, Any]]:
        """Obtiene proveedores activos ordenados por prioridad ascendente.

        Si la tabla no existe (entornos de prueba sin migraciones aplicadas),
        retorna una lista vacía y se relies en el modo legacy de Gemini.
        """
        if self._providers_cache is not None:
            return self._providers_cache
        try:
            providers = list_ai_providers()
        except Exception:  # pragma: no cover - tabla no existe en tests
            logger.warning("platform_ai_providers no disponible; usando modo legacy")
            providers = []
        self._all_providers_cache = providers
        active = [p for p in providers if p.get("active")]
        active.sort(key=lambda p: p.get("priority", 999))
        self._providers_cache = active
        return active

    def _get_all_providers(self) -> list[dict[str, Any]]:
        """Obtiene todos los registros, incluidos los inactivos."""
        if self._all_providers_cache is not None:
            return self._all_providers_cache
        try:
            providers = list_ai_providers()
        except Exception:  # pragma: no cover - tabla no existe en tests
            providers = []
        self._all_providers_cache = providers
        if self._providers_cache is None:
            active = [p for p in providers if p.get("active")]
            active.sort(key=lambda p: p.get("priority", 999))
            self._providers_cache = active
        return providers

    def _build_provider_list(self) -> list[dict[str, Any]]:
        """
        Construye la lista ordenada de proveedores a intentar.

        Incluye:
        - Proveedores de la tabla (activos, ordenados por prioridad).
        - Gemini en modo legacy (si no hay registro en la tabla y GEMINI_API_KEY está definida),
          con prioridad 0 (primero en la lista).
        """
        providers = self._get_active_providers()
        all_providers = self._get_all_providers()
        result: list[dict[str, Any]] = []

        # Gemini legacy solo cuando no existe ningún registro de Gemini (ni
        # siquiera inactivo) y hay una credencial de entorno disponible.
        gemini_registered = any(p["provider"] == "gemini" for p in all_providers)
        legacy_key = os.getenv("GEMINI_API_KEY")
        if not gemini_registered and legacy_key:
            result.append(
                {
                    "provider": "gemini",
                    "id": None,
                    "priority": 0,
                    "legacy": True,
                    "api_key": legacy_key,
                    "model": None,
                }
            )

        # Agregar proveedores de la tabla.
        for p in providers:
            result.append(
                {
                    "provider": p["provider"],
                    "id": p["id"],
                    "priority": p["priority"],
                    "legacy": False,
                    "api_key": None,  # Se resuelve justo antes de usar
                    "model": p.get("model"),
                }
            )

        return result

    def _resolve_table_credentials(self, provider_name: str) -> str | None:
        """Resuelve la credencial cifrada de la tabla para un proveedor.

        Returns:
            API key desencriptada, o None si no hay configuración o no se
            puede desencriptar (el llamador omite el proveedor).
        """
        provider_config = get_ai_provider_by_name(provider_name)
        if provider_config is None:
            logger.warning("router_provider_skip provider=%s reason=missing_config", provider_name)
            return None
        api_key = _resolve_provider_credentials(provider_name, provider_config)
        if api_key is None:
            logger.warning(
                "router_provider_skip provider=%s reason=undecryptable_credentials", provider_name
            )
        return api_key

    def _resolve_entry_api_key(self, provider_entry: dict[str, Any]) -> str | None:
        """Resuelve la API key de una entrada de la lista de proveedores.

        - Entrada legacy (Gemini por variable de entorno): usa la clave ya
          resuelta al construir la lista.
        - Entrada de tabla: resuelve y desencripta la credencial registrada.

        Returns:
            API key, o None si el proveedor debe omitirse.
        """
        if provider_entry.get("legacy", False):
            return provider_entry["api_key"]
        return self._resolve_table_credentials(provider_entry["provider"])

    @staticmethod
    def _global_timeout_result(
        provider_name: str,
        start_global: float,
        total_http_attempts: int,
        last_error: ProviderError | None,
    ) -> tuple[None, int, ProviderError]:
        """Construye el resultado cuando se agota el deadline global."""
        logger.warning(
            "router_timeout no_remaining_time provider=%s elapsed=%.1fs",
            provider_name,
            time.monotonic() - start_global,
        )
        error = last_error or ProviderError(
            "timeout", "GlobalTimeout", retry_same_provider=True, fallback_next_provider=True
        )
        return None, total_http_attempts, error

    @staticmethod
    def _classify_failure(
        error_info: ProviderError | None, provider_name: str, total_http_attempts: int
    ) -> tuple[ProviderError, bool]:
        """Normaliza el error de un proveedor y decide el fallback.

        Returns:
            Tupla (error_normalizado, continuar_con_siguiente_proveedor).
        """
        normalized = classify_provider_error(error_info or ProviderError("unknown", "UnknownError"))
        logger.warning(
            "router_fallback_decision provider=%s category=%s code=%s retry_same=%s "
            "fallback_next=%s total_http_attempts=%d",
            provider_name,
            normalized.category,
            normalized.code,
            normalized.retry_same_provider,
            normalized.fallback_next_provider,
            total_http_attempts,
        )
        return normalized, normalized.fallback_next_provider

    @staticmethod
    def _finalize_failure(
        any_provider_attempted: bool, total_http_attempts: int, last_error: ProviderError | None
    ) -> tuple[None, int, ProviderError]:
        """Construye el resultado final cuando ningún proveedor respondió."""
        if not any_provider_attempted:
            error = last_error or ProviderError("unknown", "NoProvidersAvailable")
            return None, total_http_attempts, error

        logger.error(
            "router_all_failed total_http_attempts=%d last_error_category=%s last_error_code=%s",
            total_http_attempts,
            last_error.category if last_error else "-",
            last_error.code if last_error else "-",
        )
        return (
            None,
            total_http_attempts,
            last_error or ProviderError("unknown", "AllProvidersFailed"),
        )

    def _prepare_attempt(self, provider_entry, model):
        """Resuelve credenciales, instancia el proveedor y resuelve el modelo.

        Returns:
            Tupla (provider, api_key, resolved_model) o None si hay que saltar.
        """
        api_key = self._resolve_entry_api_key(provider_entry)
        if api_key is None:
            return None
        provider = self._instantiate_provider(provider_entry["provider"])
        if provider is None:
            return None
        resolved_model = model or provider_entry.get("model")
        return provider, api_key, resolved_model

    @staticmethod
    def _provider_exception(exc, provider_name):
        """Registra la excepción del proveedor (solo tipo, sin payload ni credenciales).

        Returns:
            Tupla de (response, attempts_used, error_info).
        """
        logger.error(
            "router_provider_exception provider=%s error_type=%s",
            provider_name,
            exc.__class__.__name__,
        )
        return None, 0, ProviderError("unknown", exc.__class__.__name__)

    def _handle_attempt_result(
        self, response, error_info, provider_name, total_http_attempts, last_error
    ):
        """Procesa el resultado de un intento de proveedor.

        Returns:
            Tupla (terminal_o_None, last_error_actualizado).
            ``terminal`` None => continuar con el siguiente proveedor.
        """
        if response is not None:
            logger.info(
                "router_success provider=%s total_http_attempts=%d",
                provider_name,
                total_http_attempts,
            )
            return (response, total_http_attempts, None), last_error

        normalized, should_fallback = self._classify_failure(
            error_info, provider_name, total_http_attempts
        )
        if should_fallback:
            return None, normalized

        logger.info(
            "router_no_fallback provider=%s category=%s code=%s",
            provider_name,
            normalized.category,
            normalized.code,
        )
        return (None, total_http_attempts, normalized), normalized

    @staticmethod
    def _safe_attempts(attempts_used) -> int:
        """Normaliza el contador de intentos HTTP usados."""
        return max(0, int(attempts_used or 0))

    def generate(
        self,
        contents: Any,
        config: Any,
        business_id: int | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        model: str | None = None,
    ) -> tuple[Any | None, int, ProviderError | None]:
        """
        Ejecuta generación con fallback automático.

        Returns:
            Tupla (respuesta_o_None, solicitudes_http, ProviderError|None).
            ``solicitudes_http`` suma los intentos de red reales de cada proveedor.
        """
        provider_list = self._build_provider_list()
        if not provider_list:
            return None, 0, ProviderError("unknown", "NoProvidersAvailable")

        start_global = time.monotonic()
        deadline = start_global + _MAX_GLOBAL_TIMEOUT_S
        total_http_attempts = 0
        last_error: ProviderError | None = None
        any_provider_attempted = False

        active_count = len(provider_list)
        time_per_provider = max(4.0, min(10.0, _MAX_GLOBAL_TIMEOUT_S / active_count))

        for provider_entry in provider_list:
            provider_name = provider_entry["provider"]

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._global_timeout_result(
                    provider_name, start_global, total_http_attempts, last_error
                )

            provider_timeout = min(time_per_provider, remaining)

            prepared = self._prepare_attempt(provider_entry, model)
            if prepared is None:
                continue
            provider, api_key, resolved_model = prepared

            any_provider_attempted = True
            try:
                response, attempts_used, error_info = provider.generate(
                    contents,
                    config,
                    business_id=business_id,
                    request_id=request_id,
                    api_key=api_key,
                    timeout_s=provider_timeout,
                    deadline_s=deadline,
                    idempotency_key=idempotency_key,
                    model=resolved_model,
                )
            except Exception as exc:
                response, attempts_used, error_info = self._provider_exception(exc, provider_name)

            total_http_attempts += self._safe_attempts(attempts_used)

            terminal, last_error = self._handle_attempt_result(
                response, error_info, provider_name, total_http_attempts, last_error
            )
            if terminal is None:
                continue
            return terminal

        return self._finalize_failure(any_provider_attempted, total_http_attempts, last_error)

    def contingency_message(self) -> str:
        """Devuelve el mensaje de contingencia para mostrar al usuario."""
        return _CONTINGENCY_MESSAGE

    def clear_cache(self) -> None:
        """Limpia la caché de proveedores (útil para tests y mutaciones)."""
        self._providers_cache = None
        self._all_providers_cache = None


# Instancia singleton del router.
router = AIProviderRouter()
