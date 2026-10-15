# TurnoGo

Multi-tenant appointment booking application with AI-powered chat, built on Flask and Google Gemini.

[![CI](https://github.com/sergiosebastianalvarez2020/turnobot/actions/workflows/tests.yml/badge.svg)](https://github.com/sergiosebastianalvarez2020/turnobot/actions/workflows/tests.yml)

> **Estado:** este README describe `main`. Producción OCI puede correr un commit anterior; ver [Despliegue](#despliegue).

---

## Overview

TurnoGo is a Flask-based, multi-tenant appointment booking platform. Each business operates under its own slug (`/b/<slug>`) with isolated data. Clients can book appointments through a guided wizard or via an AI chatbot that understands natural language.

### Key Features

- **Multi-tenant architecture** — Slug-based business isolation; all queries are scoped by `business_id`
- **Landing page** — Branded public-facing page with services, hours, and booking CTA
- **Reservation wizard** — 4-step visual booking flow (service → date → details → confirm)
- **Anti-abuse contact limit** — Per-contact cap on active bookings, enforced inside the booking transaction
- **AI chatbot** — Google Gemini integration (`/chat`) with 7 tools: availability lookup, resource info, booking, turn search, cancellation, rescheduling, and human escalation
- **Conversation management** — Chat history, analytics, and human handoff escalation
- **Admin panel** — Full business management: services, schedules, appointments, users/roles, loyalty, resources, knowledge base, conversation moderation, and AI analytics
- **Metrics dashboard** — (`/admin/metricas`) Operational summary vs. previous period, top services, weekday/hour demand heatmap, new/recurrent/inactive clients, chat activity, and loyalty (no revenue or occupancy estimates)
- **Loyalty & rewards** — Points earned per completed appointment, configurable rules, rewards catalog and redemptions
- **Resource scheduling** — Bookable resources (e.g., individual barber chairs) with capacity management
- **Knowledge base** — FAQ, instructions, and policies with full-text search (PostgreSQL `tsvector`/GIN in production; SQLite FTS5 fallback)
- **Email notifications** — SMTP-based confirmation and reminder emails
- **Password recovery** — Token-based reset (`/forgot`, `/reset/<token>`)
- **Security** — CSRF tokens, scrypt password hashing, SHA-256 session tokens, security headers (incl. CSP), in-memory rate limiting
- **Health monitoring** — `/health` endpoint with database connectivity check

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 (CI matrix) |
| Framework | Flask 3.1.3 |
| AI | Google Gemini (`google-genai` 2.18.1) |
| WSGI Server | Waitress 3.0.2 |
| Database | PostgreSQL in OCI production (`psycopg` 3.2.5 + `psycopg-pool` 3.2.4); SQLite fallback remains for development/tests |
| Templates | Jinja2 |
| Rate Limiting | In-memory sliding window (shared `extensions.py` state) |
| Authentication | scrypt password hashing, SHA-256 session tokens |
| Search | PostgreSQL `tsvector` + GIN in production; SQLite FTS5 fallback |

### Dependencies

**Runtime** (`requirements.txt`): `Flask==3.1.3`, `python-dotenv==1.2.2`, `google-genai==2.18.1`, `waitress==3.0.2`, `tzdata==2025.2`, `psycopg[binary]==3.2.5`, `psycopg-pool==3.2.4`.

**Development** (`requirements-dev.txt`): `pytest==9.1.1`, `pytest-cov==7.1.0`, `ruff==0.16.8`, `mypy==2.3.1`.

## Project Structure

```
turnobot/
├── app.py                          # Facade: creates Flask app instance
├── wsgi.py                         # WSGI entry point for Waitress
├── extensions.py                   # Rate limiting, CSRF, shared globals
├── application/                    # Core (config, tenant, security, rate_limit,
│   │                               # logging, platform, frontend, requests,
│   │                               # notifications, session_crypto, json_provider,
│   │                               # public_urls, + __init__ with create_app factory)
├── routes/                         # Route modules (incl. admin, booking_abuse) +
│                                   # register_*.py URL registration via add_url_rule
├── services/                       # Domain services: ai, appointments, conversations,
│                                   # knowledge, notifications, loyalty, memberships
│                                   # (users/roles policy), metrics, platform, product
├── database/
│   ├── database.py                 # PostgreSQL connection/init + SQLite fallback
│   ├── pg_pool.py                  # PostgreSQL connection pool and compatibility layer
│   ├── pg_migrator.py              # Versioned PostgreSQL schema migrations runner
│   ├── migrator.py                 # One-time SQLite → PostgreSQL data migration
│   └── seed_auth.py                # Initial admin/superadmin seeding
├── migrations/                     # Legacy SQLite schema migrations (001–023)
├── migrations_pg/                  # Versioned PostgreSQL schema migrations
│   └── 001_initial_schema.sql      # Baseline (applied once, never edited retroactively)
├── templates/                      # Jinja2 templates (incl. admin_metricas.html)
├── static/                         # CSS & local JS served same-origin (no CDNs)
├── scripts/                        # Operational scripts (see list below)
├── tests/                          # General suite + tests_pg/ (PostgreSQL live)
├── .github/workflows/tests.yml     # CI: pytest on push/PR to main (Python 3.12, PostgreSQL 16)
├── pyproject.toml                  # pytest + coverage config
├── AGENTS.md                       # Repo conventions (lint, typecheck, tests, baselines)
├── DEPLOYMENT.md                   # Deployment & ops guide
└── CUTOVER_PROCEDURE.md            # SQLite → PostgreSQL cutover record
```

## Environment Variables

### Required to boot (production — startup raises `RuntimeError` if missing/invalid)

| Variable | Description |
|---|---|
| `FLASK_ENV` | `production` or `development` |
| `SECRET_KEY` | Flask session secret |
| `ADMIN_PASSWORD_HASH` | scrypt hash for admin access |
| `COOKIE_SECURE` | `1` for HTTPS-only cookies |
| `DATABASE_URL` | Valid PostgreSQL URL (no silent SQLite fallback in production) |
| `PUBLIC_BASE_URL` | Canonical HTTPS origin (e.g. `https://turnogo.example`) |

### Needed for specific features

| Variable | Default | Enables |
|---|---|---|
| `GEMINI_API_KEY` | — | AI chatbot (`/chat`) |
| `AI_MODEL` | `gemini-2.5-flash` | Gemini model identifier |
| `GEMINI_TIMEOUT_MS` | `15000` | Gemini API timeout |
| `GEMINI_MAX_TOTAL_CHARS` | `8000` | Max conversation history chars |
| `GEMINI_MAX_OUTPUT_TOKENS` | `1024` | Max tokens per response |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | — | Email notifications |
| `EMAIL_FROM` | `SMTP_USER` | "From" email address |
| `EMAIL_FROM_NAME` | `TurnoBot` | "From" display name |
| `INVITATION_LIFETIME_HOURS` | `72` | Invitation token expiration |

### Tuning

| Variable | Default | Description |
|---|---|---|
| `TRUSTED_PROXY_COUNT` | `0` | Reverse proxy hops to trust (0 = ignore forwarding headers) |
| `SESSION_LIFETIME_SECONDS` | `86400` | Admin session expiration (24h) |

> **Security note:** `ADMIN_PASSWORD` (plaintext) is no longer supported — use `ADMIN_PASSWORD_HASH` only. The management token hash (SHA-256) is never returned in API responses. Never publish `.env` or `/etc/turnobot.env`.

## Setup & Development

### Prerequisites

- Python 3.12+
- Dependencies from `requirements.txt` and `requirements-dev.txt`
- For `tests_pg/` and persistence-backed `tests/`: `TURNOBOT_PG_URL` pointing to PostgreSQL 16

### Installation

```bash
# 1. Clone
git clone https://github.com/sergiosebastianalvarez2020/turnobot.git
cd turnobot

# 2. Virtual environment
python -m venv venv
source venv/bin/activate       # Linux/macOS
# venv\Scripts\activate          # Windows

# 3. Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 4. Configure environment (create a .env file with the variables above)

# 5. Run (development)
python -c "from app import app; app.run(debug=True, host='127.0.0.1', port=5000)"
```

### Local Commands

```bash
# Run tests
python -m pytest                        # full suite with coverage
python -m pytest tests/test_observability.py -v  # specific file

# Lint & typecheck
python -m ruff check .
python -m ruff format --check .
python -m mypy app.py

# Health check
python scripts/check_health.py

# PostgreSQL production backup
python scripts/backup_postgresql.py
```

## Testing

- `tests/` is the general application suite. With `TURNOBOT_PG_URL` configured, its shared fixtures create isolated temporary PostgreSQL databases for tests that exercise persistence. Tests that explicitly cover SQLite compatibility may select the SQLite backend themselves.
- `tests_pg/` contains PostgreSQL integration tests (marked `pg_live`). CI runs both groups against its PostgreSQL 16 service; without `TURNOBOT_PG_URL` they skip cleanly.

Coverage includes multi-tenant isolation, booking/cancellation/rescheduling with management tokens and contact limits, rate limiting, security, AI tooling, email, knowledge base FTS, loyalty/redemptions/retention, metrics, conversations/handoff, and PostgreSQL backup tooling.

```bash
python -m pytest              # full suite with coverage (default via pyproject.toml)
python -m pytest tests/test_observability.py tests/test_conversations.py -v
python -m pytest --no-cov     # faster, no coverage
```

**Known pre-existing failures (Windows only):** two symlink tests (`test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json`, `test_prune_backups.py::...symlink_is_preserved_not_deleted`) — see `AGENTS.md`.

## Routes & API

### Public Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Landing page (default business) |
| `GET` | `/b/<slug>` | Business landing page |
| `GET` | `/b/<slug>/reservar` | Wizard — service selection |
| `GET` | `/b/<slug>/reservar/fecha` | Wizard — date selection |
| `GET` | `/b/<slug>/reservar/datos` | Wizard — customer details |
| `GET`/`POST` | `/b/<slug>/reservar/confirmar` | Wizard — confirmation |
| `GET` | `/health` | Health check (DB connectivity) |
| `POST` | `/chat` | AI chat endpoint |
| `GET` | `/api/conversations/current/messages` | Current visitor conversation |
| `GET` | `/api/conversations/<token>/messages` | Conversation history by token |
| `GET` | `/api/servicios` | List services |
| `GET` | `/api/recursos` | List resources |
| `GET` | `/api/puntos` | Loyalty points info (per-completed points, balance) |
| `GET` | `/api/disponibilidad/<fecha>` | Availability for date |
| `GET` | `/api/turnos` | List customer appointments |
| `POST` | `/api/reservar` | Book appointment |
| `POST` | `/api/cancelar` | Cancel appointment (management_token) |
| `POST` | `/api/reprogramar` | Reschedule appointment (management_token) |
| `GET`/`POST` | `/b/<slug>/turno/<token>?id=<id>` | Public turn management |

All `/api/*` and `/b/<slug>/api/*` routes support both generic and business-scoped variants.

### Authentication

| Method | Path | Description |
|---|---|---|
| `GET`/`POST` | `/login`, `/b/<slug>/login` | Admin login |
| `POST` | `/logout`, `/b/<slug>/logout` | Admin logout |
| `GET`/`POST` | `/forgot`, `/reset/<token>` | Password recovery |
| `GET`/`POST` | `/registro` | Public registration |
| `GET` | `/b/<slug>/invitacion/<token>` | Invitation acceptance |

### Administrative Endpoints (all with `/b/<slug>` variant; owner/admin only)

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin` | Admin dashboard |
| `GET` | `/admin/metricas` | Metrics dashboard |
| `POST` | `/admin/servicios/guardar` | Save service (CRUD) |
| `POST` | `/admin/servicios/<id>/estado` | Toggle service active |
| `POST` | `/admin/configuracion` | Update business settings |
| `POST` | `/admin/horarios/guardar` | Save weekly schedule |
| `POST` | `/admin/turnos/<id>/cancelar` | Cancel appointment |
| `POST` | `/admin/turnos/<id>/estado` | Update appointment status |
| `POST` | `/admin/turnos/<id>/reprogramar` | Reschedule appointment |
| `POST` | `/admin/turnos/<id>/reprogramar/con-recurso` | Reschedule with resource |
| `POST` | `/admin/turnos/crear` | Create appointment (manual) |
| `GET` | `/admin/usuarios` | User management |
| `POST` | `/admin/usuarios/invitar` | Invite user |
| `POST` | `/admin/usuarios/<id>/rol` | Change user role |
| `POST` | `/admin/usuarios/<id>/revocar` | Revoke user |
| `GET` | `/admin/recursos` | Resources dashboard |
| `POST` | `/admin/recursos/crear` | Create resource |
| `POST` | `/admin/recursos/<id>/estado` | Toggle resource active |
| `GET`/`POST` | `/admin/fidelizacion` | Loyalty dashboard |
| `POST` | `/admin/fidelizacion/configuracion` | Loyalty rules |
| `POST` | `/admin/fidelizacion/<id>/ajustar` | Adjust loyalty points |
| `POST` | `/admin/fidelizacion/recalcular` | Rebuild balances from ledger |
| `GET`/`POST` | `/admin/fidelizacion/recompensas` | Rewards catalog |
| `POST` | `/admin/fidelizacion/recompensas/<id>/estado` | Toggle reward active |
| `GET` | `/admin/conocimiento` | Knowledge base list |
| `POST` | `/admin/conocimiento/crear` | Create knowledge entry |
| `POST` | `/admin/conocimiento/<id>/editar` | Edit knowledge entry |
| `POST` | `/admin/conocimiento/<id>/eliminar` | Delete knowledge entry |
| `GET` | `/admin/conversaciones` | Conversation list |
| `GET` | `/admin/conversaciones/<id>` | Conversation detail |
| `POST` | `/admin/conversaciones/<id>/resolver` | Resolve human handoff |
| `POST` | `/admin/conversaciones/<id>/estado` | Update conversation status |
| `GET` | `/admin/inteligencia` | AI analytics dashboard |
| `GET` | `/admin/inteligencia/oportunidades` | Opportunity analytics |

### Superadmin Endpoints

`/superadmin` — Manage businesses (create, approve, deactivate), audit logs, invitation management. Requires separate superadmin credentials (bootstrap with `python scripts/create_superadmin.py`).

## AI Chatbot

`POST /chat` with 7 tools: `consultar_disponibilidad`, `consultar_recursos`, `reservar_turno`, `buscar_turnos_cliente`, `cancelar_turno`, `reprogramar_turno`, `solicitar_atencion_humana`. Guards: confirmation for mutating tools, max 5 iterations/turn, history capped at 12 messages / 2.000 chars per message / 8.000 total chars, retries with backoff, keyword- and response-based human escalation.

## Multi-Tenancy

The application supports multiple businesses via slug-based routing. All database queries are scoped by `business_id`. The tenant context is resolved from the URL slug via `application/tenant.py`:

- Business 1 (`el-corte`) is the default (no slug)
- All other businesses are accessed via `/b/<slug>`

## Rate Limiting

In-memory sliding window rate limiting (resets on restart):

| Resource | Limit |
|---|---|
| Login attempts | 10 per IP per 60s |
| Chat messages | 20 per IP/business per 60s |
| API requests | 60 per IP/endpoint/business per 60s |

State is process-local via `extensions.py`. Horizontal scaling requires shared storage (e.g., Redis).

## Database

OCI production requires a valid PostgreSQL `DATABASE_URL`; its schema is
initialized from `migrations_pg/`. Missing, empty, malformed, or non-PostgreSQL
`DATABASE_URL` values stop startup with an explicit error. Production never
falls back silently to SQLite. In development and tests, select SQLite
explicitly with `DB_BACKEND=sqlite`. The SQLite implementation (WAL mode) and
legacy migrations under `migrations/` remain available for that opt-in path.

### Schema migrations (PostgreSQL)

Startup applies `migrations_pg/001_initial_schema.sql` only to an empty
database. Incremental schema changes are versioned (`migrations_pg/00N_*.sql`)
and applied EXPLICITLY with `python scripts/migrate_pg.py` — always run
`--dry-run` first, and back up before applying:

```bash
python scripts/migrate_pg.py --dry-run
python scripts/migrate_pg.py
```

The runner records applied migrations in `schema_migrations`
(`version`, `name`, `checksum`, `applied_at`), wraps each migration in its own
transaction (rollback on failure), refuses to re-apply or to apply out of order
(checksum mismatch, gaps and a registry newer than the code all fail), adopts an
existing bootstrap baseline without re-running it **only when the 001 migration
is the only one available**, and serializes concurrent runners with the same
PostgreSQL advisory lock used by startup. Full rules (naming convention,
`CONCURRENTLY` restriction, directory security) in [DEPLOYMENT.md](DEPLOYMENT.md).

**Metadata loss protection (D2):** If the database has the 001 schema complete
but `schema_migrations` is missing **and** migrations beyond 001 are discovered,
the runner fails with a clear `MigrationError` instead of silently re-running
later migrations. Recovery requires restoring a backup or
manually reconstructing `schema_migrations`; never run the migrator against
production to diagnose state.

### Key Tables

`businesses`, `business_settings`, `services`, `weekly_schedules`, `resources`, `appointments`, `users`, `business_users`, `invitations`, `conversation_sessions`, `conversation_messages`, `conversation_analytics`, `business_knowledge`, `loyalty_settings`, `loyalty_accounts`, `points_ledger`, `rewards`, `redemptions`, `retention_actions`.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for full details.

### Production Startup

```bash
# Serve via Waitress (behind reverse proxy)
python wsgi.py
```

Waitress binds to `127.0.0.1:5000`. Expose publicly via Caddy, Nginx, or IIS with TLS. The reverse proxy must forward to `http://127.0.0.1:5000`. Production requires the boot variables above — notably a canonical HTTPS `PUBLIC_BASE_URL`, without which startup aborts.

### Health Check

```bash
python scripts/check_health.py
# OK: http://127.0.0.1:5000/health respondio HTTP 200
```

### Backups

```bash
# PostgreSQL production backup and archive-readability check
python scripts/backup_postgresql.py
python scripts/verify_backup_postgresql.py
```

`verify_backup_postgresql.py` checks that `pg_restore --list` can read the
dump; it is not a full restore test. `restore_postgresql.py` is separate,
targets a database explicitly, and refuses the production destination. The
SQLite backup, restore, verify, and prune scripts are retained as legacy
tooling and are not the production PostgreSQL backup path.

### Notifications

Appointment reminders (24h before) run via a scheduled task:

```bash
python scripts/send_reminders.py
```

Failure alerts:

```bash
python scripts/notify_failure.py
```

### Superadmin Bootstrap

```bash
python scripts/create_superadmin.py
```

## CI/CD

GitHub Actions workflow (`.github/workflows/tests.yml`) runs lint (`ruff check`), format check, typecheck (`mypy`) and the full test suite (Python 3.12, PostgreSQL 16) on every push and pull request to `main`.

## License

Proprietary. All rights reserved.
