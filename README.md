# OpsBrain AI — Social Media Manager (Backend)

Standalone FastAPI SaaS backend for the OpsBrain AI Social Media Manager. Multi-tenant
(workspace-based), plan-gated, AI-usage-metered social publishing platform: connect
Facebook/Instagram/LinkedIn/X accounts, generate on-brand posts/images/videos with Azure
OpenAI, schedule + publish, and track analytics.

This project is self-contained — it does **not** depend on `OpsBrain-Backend` or
`opsbrain-frontend` at runtime. The `app/social/` module was extracted and adapted from
`OpsBrain-Backend/app/social_media/`, with `Organization` renamed to `Workspace`
throughout.

## Stack

- **API**: FastAPI + Pydantic v2
- **DB**: PostgreSQL via SQLAlchemy 2.0 (async-free, sync sessions) + Alembic migrations
- **Auth**: JWT (python-jose) + passlib (pbkdf2_sha256) password hashing
- **Workers**: Celery + Redis (publish scheduler, analytics sync, token refresh, approval reminders)
- **AI**: Azure OpenAI — chat (text), `gpt-image-2` (images), Sora 2 (video, gated preview)
- **Storage**: Azure Blob Storage (generated/uploaded media)
- **Social OAuth**: Meta (Facebook + Instagram), LinkedIn, X (Twitter) OAuth 2.0

## Project layout

```
backend/
  app/
    main.py           # FastAPI app factory, CORS, router wiring, startup bootstrap
    core/              # settings, db session, JWT/password hashing, encryption, logging
    auth/              # register / login / me
    users/              # User model
    workspaces/         # Workspace + WorkspaceMember models, membership API
    plans/              # Plan catalog + DB overrides + AI usage ledger
    admin/              # Platform-admin-only API (users, workspaces, pricing, analytics)
    social/             # Full social media product (accounts, posts, AI gen, publishing,
                         # analytics, brand voice, templates, approvals) — mounted per workspace
    providers/          # Azure Blob storage + Azure OpenAI client factories
  workers/               # Celery app + beat schedule (social publish/analytics/maintenance)
  migrations/            # Alembic
  requirements.txt
  .env.example
  alembic.ini
```

## Getting started

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in DATABASE_URL, JWT_SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, Azure OpenAI /
# Azure Storage / Meta / LinkedIn / X credentials as needed.

./scripts/migrate.sh

# All services (api + worker + beat) — same pattern as OpsBrain-Backend:
./scripts/ecosystem.sh up
./scripts/ecosystem.sh status
./scripts/ecosystem.sh down

# API only with hot reload (foreground):
RELOAD=1 ./scripts/ecosystem.sh api

# Or manual uvicorn on port 8000 (default):
uvicorn app.main:app --reload --port 8000
```

Generate a Fernet key for `CREDENTIAL_ENCRYPTION_KEY` (used to encrypt stored OAuth
tokens at rest):

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

On startup, if `ADMIN_EMAIL` + `ADMIN_PASSWORD` are set, a platform admin user (with an
Enterprise-plan workspace) is created idempotently.

### Running the background workers

Prefer `./scripts/ecosystem.sh up` (starts api + worker + beat together).

### Production (EC2 + PM2)

API on **5000**, frontend on **5001**. Frontend folder is auto-detected as
`social-frontend` or `frontend` next to the backend repo.

```bash
cp .env.production .env
./scripts/migrate.sh

# frontend (sibling repo)
cd ../social-frontend && npm ci && npm run build && cd ../social-backend

mkdir -p .run
pm2 start scripts/pm2.ecosystem.config.cjs
pm2 save

# stop / remove
./scripts/pm2-stop.sh

# restart
./scripts/pm2-stop.sh && pm2 start scripts/pm2.ecosystem.config.cjs && pm2 save
```

Point nginx at `127.0.0.1:5000` (API) and `127.0.0.1:5001` (frontend).

Manual Celery (without PM2):

```bash
celery -A workers.celery_app:celery_app worker --loglevel=info -Q social_publish,social_analytics,social_maintenance
celery -A workers.celery_app:celery_app beat --loglevel=info
```

## API surface

All authenticated routes are under `API_V1_PREFIX` (default `/api/v1`).

- `POST /api/v1/auth/register` — create user + personal Starter workspace, returns JWT
- `POST /api/v1/auth/login` — returns JWT
- `GET  /api/v1/auth/me` — current user + workspace memberships
- `GET  /api/v1/workspaces` / `POST /api/v1/workspaces` — list / create workspaces
- `GET  /api/v1/workspaces/{id}` — workspace detail
- `GET/POST/PATCH/DELETE /api/v1/workspaces/{id}/members` — membership management
- `GET  /api/v1/plans` — effective pricing + limits (catalog merged with DB overrides)
- `GET  /api/v1/workspaces/{workspace_id}/social/...` — the full social product: accounts,
  posts, AI generation (text/image/video), scheduling, publishing, analytics, brand voice,
  templates, approvals, settings, team permissions (mirrors the original
  `OpsBrain-Backend` social API 1:1, just re-rooted under a workspace)
- `GET  /api/v1/social/oauth/{platform}/callback` (Meta/X) and
  `GET  /api/v1/social/oauth/linkedin/callback` — public OAuth provider callbacks (no
  workspace prefix, since providers redirect to a fixed URL)
- `/api/v1/admin/...` — platform-admin-only (requires `is_platform_admin`). All
  responses below are camelCase (Pydantic `alias_generator=to_camel`) to match
  the admin dashboard 1:1 — see `app/admin/schemas.py`:
  - `GET /admin/overview` — totals, MRR estimate, 30d posts/AI usage/failed
    publishes, user growth %, plan distribution
  - `GET /admin/analytics` — 30-day daily posts-published series, plan
    distribution, platform mix (published posts by platform)
  - `GET /admin/users` — paginated user directory (`?search&plan&status&page&pageSize`),
    joined with each user's primary owned workspace + social permission level
  - `POST /admin/users/{id}/suspend` (body `{suspended?: bool}` — omit to toggle),
    `POST /admin/users/{id}/plan` (body `{plan}`) — both return the updated user row
  - `GET /admin/pricing`, `PUT /admin/pricing/{plan}` — FE-shaped pricing plans
    (camelCase, `null` limit == unlimited), backed by the same `PlanOverride` table
  - Legacy/unused-by-FE: `GET/PATCH /admin/users/{id}` (snake_case), `GET
    /admin/workspaces`, `PATCH /admin/workspaces/{id}/plan|status`, `GET/PUT
    /admin/plans` (snake_case plan catalog), `GET /admin/analytics/overview`
    (snake_case totals) — all still work, kept for backwards compatibility

Interactive docs at `/docs` once the server is running.

## Plans & AI usage enforcement

`app/plans/catalog.py` defines the three tiers (Starter / Growth / Enterprise) with
concrete limits (connected accounts, posts/month, AI text/image/video generations/month,
templates, brand voice, approval workflow). Admins can override any field per-plan via
`PUT /api/v1/admin/plans/{key}` — overrides are stored in `plan_overrides` and merged
over the catalog at read time (`app/plans/service.get_effective_plan`).

Every AI generation records an `AiUsageEvent` row (`app/plans/models.py`) and is **enforced
before** the underlying Azure OpenAI call runs, in `app/social/limits.py`:

- `enforce_ai_text_limit` — gates `generate_post` + `test_brand_voice`
- `enforce_ai_image_limit` — gates `generate_image`
- `enforce_ai_video_limit` — gates `generate_video` (blocked entirely on Starter)
- `enforce_brand_voice_available` / `enforce_approval_available` — feature gates for
  Growth+ only features

All of these raise `402 Payment Required` with a structured `{code, message, limit, used}`
body when a workspace is over quota or the feature isn't included in its plan.

## Known gaps / follow-ups

- A handful of SQL index names inside `app/social/models.py` (e.g.
  `ix_social_accounts_org_id`) still contain `org` — cosmetic only, left over from the
  bulk `Organization` → `Workspace` rename since renaming index names isn't required for
  correctness. Safe to rename in a follow-up migration if desired.
- Sora 2 (video generation) and the `gpt-image-2` deployment both require Azure access
  requests/allow-listing; until approved, `generate_video` / `generate_image` will fail at
  the Azure OpenAI call (quota enforcement itself works regardless).
- OAuth apps (Meta, LinkedIn, X) need to be created/re-approved for the OpsBrain AI brand
  — the OpsBrain-Backend production app IDs are not reused here.
- No test suite was carried over from `OpsBrain-Backend`; only manual end-to-end
  verification (register → login → workspace → plans → admin → social) was performed
  against a local Postgres instance during scaffolding.
