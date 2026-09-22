# TurnoGo

Multi-tenant appointment booking application with AI-powered chat, built on Flask and Google Gemini.

[![CI](https://github.com/sergiosebastianalvarez2020/turnobot/actions/workflows/tests.yml/badge.svg)](https://github.com/sergiosebastianalvarez2020/turnobot/actions/workflows/tests.yml)

---

## Overview

TurnoGo is a Flask-based, multi-tenant appointment booking platform. Each business operates under its own slug (`/b/<slug>`) with isolated data. Clients can book appointments through a guided wizard or via an AI chatbot that understands natural language.

### Key Features

- **Multi-tenant architecture** — Slug-based business isolation; all queries are scoped by `business_id`
- **Landing page** — Branded public-facing page with services, hours, and booking CTA
- **Reservation wizard** — 4-step visual booking flow (service → date → details → confirm)
- **AI chatbot** — Google Gemini integration (`/chat`) with 7 tools: availability lookup, resource info, booking, turn search, cancellation, rescheduling, and human escalation
- **Conversation management** — Chat history, analytics, and human handoff escalation
- **Admin panel** — Full business management: services, schedules, appointments, users/roles, loyalty, resources, knowledge base, conversation moderation, and AI analytics
- **Loyalty & rewards** — Points earned per completed appointment, configurable rules, rewards catalog
- **Resource scheduling** — Bookable resources (e.g., individual barber chairs) with capacity management
- **Knowledge base** — FAQ, instructions, and policies with full-text search (FTS5)
- **Email notifications** — SMTP-based confirmation and reminder emails
- **Security** — CSRF tokens, scrypt password hashing, SHA-256 session management, security headers, in-memory rate limiting
- **Health monitoring** — `/health` endpoint with database connectivity check

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Framework | Flask 3.1.3 |
| AI | Google Gemini (`google-genai` 2.18.1) |
| WSGI Server | Waitress 3.0.2 |
| Database | SQLite (WAL mode) |
| Templates | Jinja2 |
| Rate Limiting | In-memory sliding window (shared `extensions.py` state) |
| Authentication | scrypt password hashing, SHA-256 session tokens |
| Search | SQLite FTS5 (knowledge base) |

### Dependencies

**Runtime** (`requirements.txt`):
```
Flask==3.1.3
python-dotenv==1.2.2
google-genai==2.18.1
waitress==3.0.2
tzdata==2025.2
```

**Development** (`requirements-dev.txt`):
```
pytest==9.1.1
pytest-cov==7.1.0
```

## Project Structure

```
turnobot/
├── app.py                          # Facade: creates Flask app instance
├── wsgi.py                         # WSGI entry point for Waitress
├── extensions.py                   # Rate limiting, CSRF, shared globals
├── application/                    # Core (10 modules + __init__)
│   ├── __init__.py                 # create_app() factory
│   ├── config.py                   # Configuration + env validation
│   ├── tenant.py                   # Multi-tenant context, business resolution
│   ├── security.py                 # Error handlers, security headers
│   ├── rate_limit.py               # Rate limiting wrappers
│   ├── logging_config.py           # Structured JSON logging
│   ├── platform.py                 # Platform/superadmin helpers
│   ├── frontend.py                 # Frontend config contract
│   ├── requests.py                 # Request parsing helpers
│   ├── notifications.py           # App-level notification helpers
│   └── session_crypto.py           # SHA-256 session token hashing
├── routes/                         # 17 files: 8 route modules + 8 register
│   ├── public.py                   # Landing, wizard
│   ├── public_api.py               # Public REST API
│   ├── auth.py                     # Login/logout
│   ├── auth_public.py              # Public auth (forgot, register)
│   ├── admin.py                    # Admin panel CRUD
│   ├── superadmin.py               # Superadmin panel
│   ├── invitation.py               # Invitation acceptance
│   ├── health.py                   # Healthcheck
│   └── register_*.py               # URL registration (blueprint-style)
├── services/                       # 9 domain service modules
│   ├── ai.py                       # Gemini chat with 7 tools
│   ├── appointments.py             # Booking, cancellation, rescheduling
│   ├── conversations.py            # Chat history, sessions, handoff
│   ├── knowledge.py                # FAQ/knowledge base (RAG + FTS5)
│   ├── notifications.py            # Email dispatch + reminders
│   ├── loyalty.py                  # Points & rewards
│   ├── memberships.py              # Member tiers
│   ├── platform.py                 # Platform identity
│   └── product.py                  # Onboarding state
├── database/
│   ├── database.py                 # SQLite connection + migration runner
│   ├── seed_auth.py                # Initial admin/superadmin seeding
│   └── migrations/                 # 22 SQL schema migrations (001–022)
├── templates/                      # 22 Jinja2 templates
├── static/                         # CSS & JavaScript assets
├── scripts/                        # Operational scripts
│   ├── run_production.ps1          # Production startup
│   ├── check_health.py             # HTTP healthcheck
│   ├── backup_database.py          # SQLite backup (online, WAL-safe)
│   ├── verify_backup.py            # Backup integrity verification
│   ├── restore_database.py         # Database restore
│   ├── prune_backups.py            # Backup rotation
│   ├── send_reminders.py           # 24h appointment reminders
│   ├── retry_failed_notifications.py
│   ├── create_superadmin.py        # Superadmin bootstrap
│   ├── provision_business.py       # Business provisioning
│   └── notify_failure.py           # Failure alerting
├── tests/                          # 59 test files, 787 tests
├── .github/workflows/tests.yml     # CI: pytest on push/PR
├── pyproject.toml                  # pytest + coverage config
└── DEPLOYMENT.md                   # Deployment & ops guide
```

## Environment Variables

### Required (production)

| Variable | Description |
|---|---|
| `FLASK_ENV` | `production` or `development` |
| `SECRET_KEY` | Flask session secret (required in production) |
| `ADMIN_PASSWORD_HASH` | scrypt hash for admin access (required in production) |
| `COOKIE_SECURE` | Set to `1` for HTTPS-only cookies (required in production) |
| `GEMINI_API_KEY` | Google AI API key for the chatbot |

### Optional

| Variable | Default | Description |
|---|---|---|
| `TRUSTED_PROXY_COUNT` | `0` | Number of reverse proxies (0 = disabled) |
| `SESSION_LIFETIME_SECONDS` | `86400` | Session expiration (24h) |
| `AI_MODEL` | `gemini-2.5-flash` | Gemini model identifier |
| `GEMINI_TIMEOUT_MS` | `15000` | Gemini API timeout |
| `GEMINI_MAX_TOTAL_CHARS` | `8000` | Max conversation history chars |
| `GEMINI_MAX_OUTPUT_TOKENS` | `1024` | Max tokens per response |
| `SMTP_HOST` | — | SMTP server for email notifications |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASSWORD` | — | SMTP password |
| `SMTP_FROM` | — | "From" email address |
| `INVITATION_LIFETIME_HOURS` | `72` | Invitation token expiration |
| `TRUSTED_PROXY_COUNT` | `0` | Reverse proxy hops to trust |

> **Security note:** `ADMIN_PASSWORD` (plaintext) is no longer supported — use `ADMIN_PASSWORD_HASH` only. The management token hash (SHA-256) is never returned in API responses.

## Setup & Development

### Prerequisites

- Python 3.12+
- Dependencies from `requirements.txt` and `requirements-dev.txt`

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

# 4. Configure environment
cp .env.example .env   # Edit with your values

# 5. Run (development)
python -c "from app import app; app.run(debug=True, host='127.0.0.1', port=5000)"
```

### Local Commands

```bash
# Run tests
python -m pytest                        # full suite with coverage
python -m pytest tests/test_observability.py -v  # specific file

# Lint
python -m ruff check .                  # if ruff installed
python -m mypy app.py                    # if mypy installed

# Health check
python scripts/check_health.py

# Database backup
python scripts/backup_database.py
```

## Testing

The project maintains **787 tests** across **59 test files** covering:

- Multi-tenant isolation (each business's data is isolated)
- Appointment booking/cancellation/rescheduling with management tokens
- Rate limiting (API, chat, login)
- Security (CSRF, password hashing, security headers, token validation)
- AI chatbot tooling and error handling
- Email notifications
- Knowledge base (FTS5 search)
- Loyalty points and membership tiers
- Conversation management and human handoff
- Backup/restore integrity
- Onboarding and provisioning

```bash
# Run with coverage (default via pyproject.toml)
python -m pytest

# Run specific tests
python -m pytest tests/test_observability.py tests/test_conversations.py -v

# Run without coverage for speed
python -m pytest --no-cov
```

**Known pre-existing failures (Windows only):**

| Test | Cause |
|---|---|
| `tests/test_admin_panel.py::TestAdminAJAX::test_admin_reschedule_returns_json` | Windows symlink limitation |
| `tests/test_prune_backups.py::TestPrunePlan::test_symlink_is_preserved_not_deleted` | Windows symlink limitation |

## Routes & API

### Public Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Landing page (default business) |
| `GET` | `/b/<slug>` | Business landing page |
| `GET` | `/b/<slug>/reservar` | Reservation wizard — service selection |
| `GET` | `/b/<slug>/reservar/fecha` | Reservation wizard — date selection |
| `GET` | `POST` | `/b/<slug>/reservar/datos` | Reservation wizard — customer details |
| `GET` | `POST` | `/b/<slug>/reservar/confirmar` | Reservation wizard — confirmation |
| `GET` | `/health` | Health check (DB connectivity) |
| `POST` | `/chat` | AI chat endpoint |
| `GET` | `/api/conversations/<token>/messages` | Conversation history |
| `GET` | `/api/servicios` | List services |
| `GET` | `/api/recursos` | List resources |
| `GET` | `/api/puntos` | List business locations |
| `GET` | `/api/disponibilidad/<fecha>` | Availability for date |
| `GET` | `/api/turnos` | List customer appointments |
| `POST` | `/api/reservar` | Book appointment |
| `POST` | `/api/cancelar` | Cancel appointment (management_token) |
| `POST` | `/api/reprogramar` | Reschedule appointment (management_token) |
| `GET` | `POST` | `/b/<slug>/turno/<token>?id=<id>` | Public turn management |

All `/api/*` and `/b/<slug>/api/*` routes support both generic and business-scoped variants.

### Authentication

| Method | Path | Description |
|---|---|---|
| `GET`/`POST` | `/login` | Admin login |
| `GET`/`POST` | `/b/<slug>/login` | Slug-scoped admin login |
| `POST` | `/logout` | Admin logout |
| `POST` | `/b/<slug>/logout` | Slug-scoped admin logout |

### Administrative Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin` | Admin dashboard |
| `GET` | `/b/<slug>/admin` | Business admin dashboard |
| `POST` | `/admin/servicios/guardar` | Save service (CRUD) |
| `POST` | `/admin/servicios/<id>/estado` | Toggle service active |
| `POST` | `/admin/configuracion` | Update business settings |
| `POST` | `/admin/horarios/guardar` | Save weekly schedule |
| `POST` | `/admin/turnos/<id>/cancelar` | Cancel appointment |
| `POST` | `/admin/turnos/<id>/estado` | Update appointment status |
| `POST` | `/admin/turnos/<id>/reprogramar` | Reschedule appointment |
| `POST` | `/admin/turnos/crear` | Create appointment (manual) |
| `GET` | `/admin/usuarios` | User management |
| `POST` | `/admin/usuarios/invitar` | Invite user |
| `POST` | `/admin/usuarios/<id>/rol` | Change user role |
| `POST` | `/admin/usuarios/<id>/revocar` | Revoke user |
| `GET`/`POST` | `/admin/fidelizacion` | Loyalty dashboard |
| `POST` | `/admin/fidelizacion/configuracion` | Loyalty rules |
| `POST` | `/admin/fidelizacion/<id>/ajustar` | Adjust loyalty points |
| `GET`/`POST` | `/admin/fidelizacion/recompensas` | Rewards catalog |
| `POST` | `/admin/recursos/crear` | Create resource |
| `GET` | `/admin/conocimiento` | Knowledge base |
| `GET` | `/admin/conversaciones` | Conversation list |
| `GET` | `/admin/conversaciones/<id>` | Conversation detail |
| `POST` | `/admin/conversaciones/<id>/resolver` | Resolve human handoff |
| `GET` | `/admin/inteligencia` | AI analytics dashboard |
| `GET` | `/admin/inteligencia/oportunidades` | Opportunity analytics |

### Superadmin Endpoints

`/superadmin` — Manage businesses (create, approve, deactivate), audit logs, invitation management. Requires separate superadmin credentials.

## AI Chatbot

The chatbot at `POST /chat` integrates with Google Gemini and provides 7 tools:

| Tool | Description |
|---|---|
| `consultar_disponibilidad` | Query available time slots by date/service/resource |
| `consultar_recursos` | List bookable resources |
| `reservar_turno` | Book a new appointment (requires phone verification) |
| `buscar_turnos` | Search existing appointments by phone |
| `cancelar_turno` | Cancel an appointment (requires management_token) |
| `reprogramar_turno` | Reschedule an appointment (requires management_token) |
| `solicitar_atencion_humana` | Escalate to human — marks session as `needs_human` |

**Safety guards:**
- Mutating tools (`reservar_turno`, `cancelar_turno`, `reprogramar_turno`) require confirmation — plain-text claims are neutralized
- Max 5 tool iterations per chat turn
- Conversation history capped at 12 messages / 2,000 chars per message
- Transient Gemini errors (timeout, quota, UNAVAILABLE) retried with backoff
- Automatic human escalation detection (keyword-based) and response-based detection

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

SQLite with WAL mode. 22 schema migrations are applied automatically on startup via `database/database.py:init_database()`.

### Key Tables

| Table | Description |
|---|---|
| `businesses` | Business (id, name, slug, active) |
| `business_settings` | Configuration (name, type, timezone, branding, etc.) |
| `services` | Service catalog (name, price, duration) |
| `weekly_schedules` | Weekly open hours |
| `resources` | Bookable resources (chair, machine, etc.) |
| `appointments` | Customer appointments (with `management_token_hash`) |
| `users` | Admin/staff users (scrypt-hashed passwords) |
| `invitations` | User invitations (token hash, expiry) |
| `conversation_sessions` | Chat sessions (status, needs_human flag) |
| `conversation_messages` | Chat message history |
| `conversation_analytics` | Aggregate question analytics |
| `business_knowledge` | FAQ/instructions/policies |
| `loyalty_accounts` | Customer loyalty points |
| `loyalty_rules` | Point earning rules |
| `loyalty_rewards` | Reward catalog items |

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for full details.

### Production Startup

```bash
# Activate venv
source venv312/bin/activate

# Serve via Waitress (behind reverse proxy)
python wsgi.py
```

Waitress binds to `127.0.0.1:5000`. Expose publicly via Caddy, Nginx, or IIS with TLS. The reverse proxy must forward to `http://127.0.0.1:5000`.

### Health Check

```bash
python scripts/check_health.py
# OK: http://127.0.0.1:5000/health responded HTTP 200
```

### Backups

```bash
# Backup (uses SQLite online backup API — WAL-safe)
python scripts/backup_database.py

# Restore (stop app first)
python scripts/restore_database.py database/backups/appointments-YYYYMMDD-HHMMSS.db

# Verify backup integrity
python scripts/verify_backup.py database/backups/appointments-YYYYMMDD-HHMMSS.db

# Prune old backups
python scripts/prune_backups.py
```

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

GitHub Actions workflow (`.github/workflows/tests.yml`) runs the full test suite on every push and pull request to `main`.

## License

Proprietary. All rights reserved.
