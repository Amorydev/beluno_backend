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
uv run python scripts/generate_signing_key.py --with-token-hash-key >> .env

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
`docs/contracts/sync-protocol.md`. Operators use `scripts/jobs.py` for dead
letters and `docs/runbooks/sync-operations.md` for metrics and retention.

## Finance

Each plan has an append-only ledger in minor units (`docs/adr/0003-financial-ledger.md`):
expenses with immutable revisions, refunds, settlements and waivers, budgets,
cost commitments, and a virtual fund that holds no real money. Endpoints live
under `/v1/plans/{plan_id}/` (`expenses`, `settlements`, `waivers`, `ledger`,
`budgets`, `commitments`, `fund`) plus `/v1/currencies`; sync carries them as
plan-scope entities. Operators use `scripts/finance.py` and
`docs/runbooks/finance-operations.md`; `BELUNO_FINANCE_WRITES_ENABLED=false`
stops every financial write while reads stay available.

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
