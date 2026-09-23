# ADR-0005: Tipado incremental con mypy

- **Estado:** Aceptado
- **Fecha:** 2026
- **Contexto:** El proyecto creció con código no tipado (dicts, filas de
  `sqlite3`, `flask.g`, esquemas Gemini). Correr mypy sobre la app completa la
  primera vez arroja ruido de módulos sin anotar (redefiniciones, vars sin
  anotar, `Any` implícitos) sin guiarlo hacia un punto de partida estable.

## Decisión

Configurar `mypy` de forma **incremental** en `pyproject.toml`:

- `python_version = "3.12"`, `warn_unused_ignores = true`.
- `files = ["services/product.py"]`: solamente los módulos listados exigen
  anotaciones completas (`disallow_untyped_defs = true` +
  `check_untyped_defs = true` vía overrides por módulo).
- El resto del proyecto sigue sin exigencias hasta que se cubra módulo a
  módulo, priorizando los módulos puros (sin importar `flask`) para evitar
  falsos positivos de `Any`.

## Criterios de cobertura

1. Empezar por `services/product.py` (módulo pequeño y sin dependencias de
   framework) como módulo semilla ya tipado.
2. Extender en fases: cada módulo que se incorpore debe quedar tipado al 100%
   (sin `# type: ignore` a menos que se justifique en comentario).
3. El CI valida `python -m mypy` (usa `files` de la config) en cada push/PR.

## Consecuencias

- `mypy` da 0 errores y no bloquea el trabajo en módulos no tipados.
- El tipado progresivo habilita el ADR-0004 (reemplazar `except Exception` por
  excepciones específicas con conocimiento de tipos).
- Los hallazgos de mypy fuera de alcance (ej. redefinición de `_row_to_dict`,
  ya resuelta, o el shim `_EMAIL_RE`) se resuelven a medida que cada módulo
  entra en alcance.