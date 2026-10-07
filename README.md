# Beluno Backend

Python backend for **Beluno — Plans with your people**.

The service is a modular monolith built with FastAPI, PostgreSQL, SQLAlchemy,
Alembic, and Procrastinate. The API is its own identity provider, issues ES256
access JWTs, and manages all domain writes; clients never write tables directly.

## Local setup

```bash
uv sync --all-groups
cp .env.example .env

# Generate signing keys and token hash key for local identity (ADR 0007)
uv run python scripts/generate_signing_key.py --with-token-hash-key --with-booking-keys >> .env

# Start a disposable PostgreSQL instance
docker compose -f docker-compose.test.yml up -d

# Bootstrap database with runtime roles
uv run python scripts/bootstrap_database.py

# Run API in watch mode
uv run uvicorn beluno.api.main:app --reload
```

Email codes print to the console in development. SMTP is configured only for
staging/production environments.

Run foundation checks:

```bash
uv run ruff check .
uv run mypy src
uv run coverage run -m pytest  # Uses Testcontainers if Docker runs, else skips live-DB tests
uv run coverage report --fail-under=80
uv run python scripts/export_openapi.py
```

To run live-database integration tests against your local PostgreSQL, set:

```bash
export BELUNO_TEST_ADMIN_DATABASE_URL=postgresql+psycopg://migrator:migrator_password@localhost:54329/beluno_test
uv run pytest
```

To dry-run migrations and verify SQL:

```bash
uv run alembic upgrade head --sql
```

## Trips, hangouts, and crews

The product is trip-first (`docs/adr/0008-trip-first-realignment.md`). A plan
(`/v1/plans`) is a `trip` (destinations, pass colour, expected size) or a
`hangout` (optional activity). Participants carry a default share, an avatar
colour, and capabilities a manager can grant to members (`expenses.manage`,
`budgets.manage`). People join through invite links (`/v1/invites`). Crews
(`/v1/crews`) are private, saved lists of people a user plans with; a new plan
can start from one.

## Sync and reliability

Every mutation is a catalog command (`src/beluno/api/commands`) that REST and
`POST /v1/sync/push` share: optional `Idempotency-Key` on REST, `operation_id`
on push, stored outcomes replayed for repeats. Clients discover their scopes with
`POST /v1/sync/handshake` and read changes with `POST /v1/sync/pull`; see
`docs/contracts/sync-protocol.md`. An activity feed tracks what changed in each
plan and person's account as typed events (never free text) in plan and user scopes,
synced and retained 180 days. Operators use `scripts/jobs.py` for dead
letters and `docs/runbooks/sync-operations.md` for metrics and retention.

## Account management

People can delete their account (`DELETE /v1/me`) after a recent sign-in (guests any time).
Deletion is immediate and permanent and leaves money history intact: the person
becomes "Former member" in every plan, and others can still settle with them. It
is refused while they own a plan another person is still in (transfer ownership first;
a guest must create an account to take it). Placeholders never block. A deleted plan
can be restored for 30 days (`BELUNO_PLAN_PURGE_AFTER_DAYS`); then it is purged
with everything it holds.
See `docs/adr/0009-activity-feed-and-account-deletion.md`.

## Trip planning

Trips have a plan: saved places (`/v1/plans/{id}/places`, with "want to go"
reactions and Maps links read offline for coordinates) and an itinerary
(`/v1/plans/{id}/itinerary`: items on a day or anytime, ordered within the day,
with local times, a lead, attendance, and an estimated cost that budgets count
until an expense pays it), and polls (`/v1/plans/{id}/polls`: single choice or
yes/no with a quorum, an optional deadline, open votes, one result however it
closes, and outcome actions that save the winning place or put it on the
itinerary), and bookings (`/v1/plans/{id}/bookings`: kind, provider, local start
and end, travelers, price counted once in budgets, payment note, free-cancellation
deadline). A booking's confirmation code and private notes are sealed at rest
(AES-GCM, `BELUNO_BOOKING_KEYS`), never listed or synced, and revealed only to its
travelers, its creator, and organisers through an audited, rate-limited
`POST .../bookings/{booking_id}/reveal`. Tasks (`/v1/plans/{id}/tasks`) have one
assignee, a due date and optional local time, and a status the assignee moves.
Packing (`/v1/plans/{id}/packing`) has a shared list anyone ticks and a private list
per person, plus client-supplied templates applied once per list
(`POST .../packing/templates`). Sync carries them as `place`, `itinerary_item`,
`poll`, `booking`, `task`, and `packing_item` (private items in the owner's user
scope).

## Finance

Each plan has an append-only ledger in minor units (`docs/adr/0003-financial-ledger.md`):
expenses with immutable revisions, refunds, settlements and waivers, budgets,
cost commitments, and a virtual fund that holds no real money. The base currency
is changeable at a frozen rate; consolidation moves all balances to the base
currency. Endpoints live under `/v1/plans/{plan_id}/` (`expenses`, `settlements`,
`waivers`, `ledger`, `budgets`, `commitments`, `fund`) plus `/v1/currencies` and
`/v1/fx/rates`; sync carries them as plan-scope entities. Operators use
`scripts/finance.py` and `docs/runbooks/finance-operations.md`;
`BELUNO_FINANCE_WRITES_ENABLED=false` stops every financial write while reads
stay available.

## Runtime

The service has three concurrent processes (one database URL per process):

```bash
uv run uvicorn beluno.api.main:app              # HTTP API, synchronous domain commands
uv run python -m beluno.worker.main            # Idempotent background job consumer (Procrastinate)
uv run python -m beluno.scheduler.main         # Periodic job scheduler
```

Worker and scheduler require `BELUNO_WORKER_DATABASE_URL` and
`BELUNO_SCHEDULER_DATABASE_URL`. Database credentials never belong in the
repository.

One container image (`Dockerfile`) runs every process, and `deploy/staging/`
holds a compose stack with OTel, Prometheus alerts, and Grafana. See
`docs/runbooks/deploy-rollback.md` (deploy, rollback, kill switches),
`docs/runbooks/performance.md` (targets and load tests), and
`docs/runbooks/database-drills.md` (backup, restore, migration rehearsal), plus
`docs/adr/0010-release-one-operations.md`.
