# ADR-0007: Dispatch de herramientas de IA por handlers

- **Estado:** Aceptado
- **Fecha:** 2026
- **Contexto:** `execute_tool` en `services/ai.py` era un único bloque de ~280
  líneas con una cadena de `if name == "..."` (7 herramientas). Cada rama
  combinaba validación, acceso a BD, formateo y manejo de errores, y el
  `solicitar_atencion_humana` ya estaba extraído pero seguía envuelto con su
  propio try/except en el dispatch.

## Decisión

Refactor estructural (sin cambios de comportamiento):

1. Cada herramienta es una función `_execute_<herramienta>(arguments, business_id, session_id=None)`
   que contiene su proprio `try/except` + `logger.exception` + respuesta de
   error (idéntica a la original).
2. Un dict `_TOOL_HANDLERS` mapea nombre de herramienta → función.
3. `execute_tool` queda como dispatch fino: valida `business_id`, busca en el
   dict y delega. Para herramientas desconocidas conserva el comportamiento
   histórico (retorna `None`), sin inventar un fallback nuevo.

Se aprovechó la misma pasada para deduplicar el formateo de fecha humana
(day-of-week + `dd/mm/aaaa` en español) que estaba repetido en 3 funciones:
ahora `_format_fecha_humana(fecha)` lo centraliza y el fallback de fechas
inválidas se mantiene por llamador.

## Consecuencias

- `execute_tool` pasa de ~290 líneas a 8; cada handler es testeable y legible
  de forma aislada.
- La suite (incluidos `test_observability`, `test_notifications_conditional`,
  `test_chat_recovery`, `test_emails_etapa4`) valida que los resultados de cada
  herramienta no cambiaron.
- Agregar una herramienta nueva es: función + entrada en `_TOOL_HANDLERS`.