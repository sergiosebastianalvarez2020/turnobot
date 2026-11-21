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
from services.ai_providers.base import AIProvider
from services.ai_providers.gemini import GeminiProvider
from services.ai_providers.groq import GroqProvider

logger = logging.getLogger("turnobot.ai.router")

# Intentos máximos por proveedor cuando el error permite reintento.
_MAX_ATTEMPTS_PER_PROVIDER = 2

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
    }

    def __init__(self) -> None:
        self._providers_cache: list[dict[str, Any]] | None = None

    def _instantiate_provider(
        self, provider_name: str, api_key: str | None = None
    ) -> AIProvider | None:
        """Crea una instancia del proveedor con la API Key dada."""
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
        active = [p for p in providers if p.get("active")]
        active.sort(key=lambda p: p.get("priority", 999))
        self._providers_cache = active
        return active

    def _build_provider_list(
        self, contents: Any, config: Any, business_id: int | None
    ) -> list[dict[str, Any]]:
        """
        Construye la lista ordenada de proveedores a intentar.

        Incluye:
        - Proveedores de la tabla (activos, ordenados por prioridad).
        - Gemini en modo legacy (si no hay registro en la tabla y GEMINI_API_KEY está definida),
          con prioridad 0 (primero en la lista).
        """
        providers = self._get_active_providers()
        result: list[dict[str, Any]] = []

        # Verificar si Gemini tiene registro en la tabla.
        gemini_in_table = any(p["provider"] == "gemini" for p in providers)

        # Si Gemini no está en la tabla, agregarlo como legacy (prioridad 0).
        # La API Key se resuelve en el momento de usar (variable de entorno GEMINI_API_KEY).
        if not gemini_in_table:
            result.append(
                {
                    "provider": "gemini",
                    "id": None,
                    "priority": 0,
                    "legacy": True,
                    "api_key": os.getenv("GEMINI_API_KEY"),
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
                }
            )

        return result

    def generate(
        self,
        contents: Any,
        config: Any,
        business_id: int | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Any | None, int, tuple[str, str] | None]:
        """
        Ejecuta generación con fallback automático.

        Returns:
            Tupla (respuesta_o_None, total_intentos, informacion_de_error_o_None).
        """
        provider_list = self._build_provider_list(contents, config, business_id)
        if not provider_list:
            return None, 0, ("unknown", "NoProvidersAvailable")

        start_global = time.monotonic()
        deadline = start_global + _MAX_GLOBAL_TIMEOUT_S
        total_attempts = 0
        last_error_info: tuple[str, str] | None = None
        responding_provider: str | None = None
        any_provider_attempted = False

        # Calcular cuántos proveedores activos hay para distribuir el presupuesto.
        active_count = len(provider_list)
        if active_count == 0:
            return None, 0, ("unknown", "NoProvidersAvailable")

        # Tiempo base por proveedor: presupuesto dividido entre proveedores,
        # con un mínimo de 4s y un máximo de 10s por llamada individual.
        time_per_provider = max(4.0, min(10.0, _MAX_GLOBAL_TIMEOUT_S / active_count))

        for provider_entry in provider_list:
            provider_name = provider_entry["provider"]
            is_legacy = provider_entry.get("legacy", False)

            # Calcular tiempo restante antes de iniciar este proveedor.
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                logger.warning(
                    "router_timeout no_remaining_time provider=%s elapsed=%.1fs",
                    provider_name,
                    time.monotonic() - start_global,
                )
                if responding_provider is None:
                    return None, total_attempts, ("timeout", "GlobalTimeout")
                break

            # El timeout para este proveedor es el menor entre el tiempo por
            # proveedor asignado y el tiempo restante en el presupuesto global.
            provider_timeout = min(time_per_provider, remaining)

            # Resolver credenciales justo antes de usar el proveedor.
            if is_legacy:
                api_key = provider_entry["api_key"]
                # Modo legacy: intentar aunque no haya API Key; el proveedor
                # manejará el error de autenticación si corresponde.
            else:
                provider_config = get_ai_provider_by_name(provider_name)
                if provider_config is None:
                    continue
                api_key = _resolve_provider_credentials(provider_name, provider_config)
                if api_key is None:
                    continue

            # Instanciar proveedor.
            provider = self._instantiate_provider(provider_name, api_key)
            if provider is None:
                continue

            any_provider_attempted = True

            # Intentar hasta _MAX_ATTEMPTS_PER_PROVIDER veces.
            for attempt in range(1, _MAX_ATTEMPTS_PER_PROVIDER + 1):
                total_attempts += 1

                # Calcular tiempo restante antes de cada intento.
                remaining = deadline - time.monotonic()
                if remaining <= 1.0:
                    logger.warning(
                        "router_timeout no_remaining_time provider=%s attempt=%d",
                        provider_name,
                        attempt,
                    )
                    if responding_provider is None:
                        return None, total_attempts, ("timeout", "GlobalTimeout")
                    break

                # Timeout para este intento: no exceder el tiempo restante del deadline.
                attempt_timeout = min(provider_timeout, remaining)

                try:
                    response, attempt_used, error_info = provider.generate(
                        contents,
                        config,
                        business_id=business_id,
                        request_id=request_id,
                        api_key=api_key if not is_legacy else None,
                        timeout_s=attempt_timeout,
                        deadline_s=deadline,
                        idempotency_key=idempotency_key,
                    )
                except Exception as exc:  # pragma: no cover - errores inesperados del proveedor
                    logger.exception("Error inesperado en proveedor %s", provider_name)
                    error_info = ("unknown", exc.__class__.__name__)
                    response = None

                if response is not None:
                    responding_provider = provider_name
                    logger.info(
                        "router_success provider=%s attempts=%d responding_provider=%s",
                        provider_name,
                        total_attempts,
                        responding_provider,
                    )
                    return response, total_attempts, None

                # Error: clasificar.
                category, error_type = error_info or ("unknown", "UnknownError")
                last_error_info = (category, error_type)

                logger.warning(
                    "router_fallback provider=%s attempt=%d category=%s error_type=%s",
                    provider_name,
                    attempt,
                    category,
                    error_type,
                )

                # Si el error no es transitorio, no cambiar de proveedor (fail fast).
                # Las categorías transitorias incluyen errores de autenticación
                # (401/403) para permitir fallback al siguiente proveedor cuando
                # una credencial es inválida o revocada.
                _TRANSIENT_CATEGORIES = {"timeout", "quota", "server", "auth"}
                is_transient = category in _TRANSIENT_CATEGORIES
                if not is_transient:
                    logger.info(
                        "router_no_fallback provider=%s reason=non_transient_error category=%s",
                        provider_name,
                        category,
                    )
                    return None, total_attempts, last_error_info

                # Error transitorio: probar el siguiente proveedor.
                break

        # Todos los proveedores fallaron.
        if responding_provider is None:
            logger.error(
                "router_all_failed total_attempts=%d last_error=%s", total_attempts, last_error_info
            )
            if not any_provider_attempted:
                return None, total_attempts, ("unknown", "NoProvidersAvailable")
            return None, total_attempts, last_error_info or ("unknown", "AllProvidersFailed")

        return None, total_attempts, last_error_info

    def contingency_message(self) -> str:
        """Devuelve el mensaje de contingencia para mostrar al usuario."""
        return _CONTINGENCY_MESSAGE

    def clear_cache(self) -> None:
        """Limpia la caché de proveedores (útil para tests)."""
        self._providers_cache = None


# Instancia singleton del router.
router = AIProviderRouter()
