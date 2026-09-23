# ADR-0004: Auditoría de `except Exception`

- **Estado:** Aceptado
- **Fecha:** 2026
- **Contexto:** El código usa `except Exception` en 53 lugares (13 módulos).
  Ruff marca cada ocurrencia como `BLE001`. Sin auditar, un `except Exception`
  puede ocultar errores reales de programa (`NameError`, `KeyError`, bugs de
  lógica) como si fueran fallas esperadas. En esta fase NO se cambia el
  comportamiento observado, así que la auditoría clasifica cada caso y solo se
  remueven los `except Exception` cuando son claramente inofensivos.

## Inventario (53 en total)

| Archivo                        | Cantidad | Rol principal                                                                 |
| ------------------------------ | -------- | ----------------------------------------------------------------------------- |
| `services/ai.py`               | 16       | envoltorios de herramientas y de Gemini: `logger.exception` + respuesta amable |
| `services/logging_config.py`   | 7        | hooks de logging (no deben tumbar la app)                                     |
| `routes/public_api.py`         | 7        | endpoints públicos: respuesta JSON de error genérico                          |
| `database/database.py`         | 6        | operaciones SQL + `logger.exception`                                          |
| `services/appointments.py`     | 4        | reserva/cancelación/reprogramación: traduce error a razón de negocio          |
| `services/loyalty.py`          | 4        | ledger/ajustes: rollback + mensaje de error                                   |
| `services/notifications.py`    | 3        | envíos de email: nunca bloquear la reserva                                    |
| resto (1 c/u)                  | 6        | tenant.py, auth_public.py, health.py, public.py, check_health.py, notify_failure.py |

## Criterios

1. Se **conservan** los `except Exception` que:
   - registran con `logger.exception` y devuelven una respuesta de fallback al
     usuario (bot/IA, endpoints públicos), o
   - ejecutan un rollback transaccional y re-levantan con `raise ... from None`
     un error de dominio, o
   - son hooks de framework cuyos fallos no deben derribar el proceso.
2. Se **removieron** (en esta fase) los que enmascaraban código muerto o
   variables sin uso, y los bloques laterales sin efecto.
3. No se reemplaza `except Exception` por excepciones específicas mientras no
   se conozca el set exacto que pueden lanzar los dependientes; eso queda como
   trabajo futuro (ADR-0005 extiende la cobertura de tipos que lo habilita).

## Decisión

Mantener `except Exception` + `logger.exception` como patrón en los puntos de
frontera (IA, API pública, hooks), anotado en la config de Ruff
(`ignore = ["BLE001", ...]` en `pyproject.toml`) con referencia a este ADR.
Prohibir su uso en lógica de negocio nueva salvo que sea punto de frontera.

## Consecuencias

- La lint pasa limpia sin sacrificar el patrón defensivo de frontera.
- El riesgo de errores silenciados se mitiga con el logging siempre presente.
- Migración progresiva a excepciones específicas cuando crezca la cobertura de
  tipos (ADR-0005).