# AGENTS.md — Project Guidelines

## Lint & Typecheck

- Python lint: `python -m ruff check .` (if ruff is installed)
- Typecheck: `python -m mypy app.py` (if mypy is installed)

## Test

- Run tests: `python -m pytest tests/ -v --tb=short`
- Coverage is enabled by default via pyproject.toml configuration

## Pre-existing Test Failures (Baseline)

The following tests fail on Windows due to platform limitations, unrelated to feature work:

1. `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json`
   - Failure cause: Windows does not support symlinks without elevated privileges
   - This test relies on symlink behavior for appointment rescheduling

2. `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted`
   - Failure cause: Windows symlink limitations (same as above)
   - This test validates backup symlink preservation during prune operations

**Expected baseline:** 732 passed, 2 failed

## Note on Flaky Windows Tests

On Windows, two categories of tests are known to fail intermittently due to platform limitations:

1. **Symlink tests** — Windows does not support symlinks without elevated privileges:
   - `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json` (may fail when symlinks not available)
   - `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted`

2. **Concurrency tests** — threading/transaction timing differences on Windows:
   - `tests/test_security_concurrency.py::AtomicSecurityTests::test_email_claim_allows_one_worker_and_retry_after_failure`

The count of passing/failing tests may vary between runs depending on Windows environment state, but the total remains stable.

## Blueprint Migration Notes

- The `name=''` parameter in `app.register_blueprint()` is reserved for `auth_bp` (Paso 4)
- The `/health` endpoint is registered via `app.add_url_rule()` to avoid consuming `name=''`
- The `/` and `/b/<slug>` routes are registered via `app.add_url_rule()` to preserve global endpoint names (`index`, `business_index`)
- `routes/public.py` = módulo de rutas (NOT a registered Blueprint)
- `routes/health.py` = módulo de ruta (Blueprint definido pero NO registrado)
- Blueprint registration is handled in `routes/__init__.py:register_blueprints(app)`
