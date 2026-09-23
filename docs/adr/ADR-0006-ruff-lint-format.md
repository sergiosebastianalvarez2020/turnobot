# ADR-0006: Ruff como linter y formatter unificado

- **Estado:** Aceptado
- **Fecha:** 2026
- **Contexto:** El proyecto no tenía linter ni formatter configurados. La
  primera pasada de `ruff check` reportó 3+ decimales de errores (imports sin
  ordenar, líneas largas, vars sin uso) y `ruff format` normalizó 102 de 121
  archivos. Una primera pasada `ruff --fix` removió un import re-exportado
  (`notification_sent_scoped` en `services/notifications.py`) que los tests
  usan como API pública: las re-exportaciones deliberadas se marcan con
  `# noqa: F401` comentado.

## Decisión

Vincular el proyecto a Ruff (lint + format) con la config en `pyproject.toml`:

- `target-version = "py312"`, `line-length = 100`.
- Lint select: `E, F, I, UP, B, W`.
- Ignored: `E501` (lo resuelve el formatter), `BLE001` (ver ADR-0004),
  `B009`, `B017` y `B905` (patrones deliberados en tests: `assertRaises(Exception)`
  y `zip()` sobre snapshots de migraciones).
- `isort.split-on-trailing-comma = false` + `format.skip-magic-trailing-comma = true`
  para que lint y format no se contradigan.
- `per-file-ignores` para bootstraps deliberados (`load_dotenv()`, `sys.path`)
  y re-exports de `app.py`/`application/__init__.py`.
- Excludes: `venv, venv312, .kilo, .github, logs, database/backups, get_inventory.py`.

## Consecuencias

- `ruff check .` y `ruff format --check .` pasan limpios en 121 archivos.
- El formato queda aplicado a todo el árbol; los futuros cambios se mantienen
  formateados vía pre-commit (`.pre-commit-config.yaml`) y CI
  (`.github/workflows/tests.yml`).
- Regla: las importaciones que son re-export intencional llevan `# noqa: F401`
  con explicación, para que el autofix nunca las vuelva a borrar.