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

Accounts sign in with Google, Apple, or an emailed code or link, and can add passkeys
from the Me screen after a recent sign-in (`POST /v1/me/passkeys/registration-options`,
then `POST /v1/me/passkeys`). A passkey then signs in or steps up
(`POST /v1/auth/passkey/options`, then `POST /v1/auth/passkey`); as a guest it claims
the passkey's account. A passkey never creates an account. Passkeys require user
verification, keep no attestation, and need `BELUNO_WEBAUTHN_RP_ID` (the app's
domain) and `BELUNO_WEBAUTHN_ORIGINS` in staging and production.

People sign out every other device at once with `POST /v1/me/sessions/sign-out-others`.
People can delete their account (`DELETE /v1/me`) after a recent sign-in (guests any time).
Deletion is immediate and permanent and leaves money history intact: the person
becomes "Former member" in every plan, and others can still settle with them. It
is refused while they own a plan another person is still in (transfer ownership first;
a guest must create an account to take it). Placeholders never block. A deleted plan
can be restored for 30 days (`BELUNO_PLAN_PURGE_AFTER_DAYS`); then it is purged
with everything it holds.
See `docs/adr/0009-activity-feed-and-account-deletion.md`.

Exports are free and downloaded at once (nothing is stored): anyone in a plan gets
`GET /v1/plans/{id}/export?format=csv` (one row per expense, original amount and
base-currency snapshot; voided ones keep their amounts with `state` = `voided`) or `format=json` (every entity they sync), and
`GET /v1/me/export` returns the account's own data and every plan it is still in.
Exports hold exactly what the caller can already sync, so booking secrets, invite
tokens, and other people's private packing items never appear; each is audited and
rate-limited.
With a Trip Pass (or the owner's Pro; hangouts are free) two more formats open:
`format=accounting`, a CSV with one row per person and journal entry whose `amount`
adds up to the balances, and `format=pdf`, the trip report (spending by category, each
person's paid and share, who pays whom, every expense with a receipt mark), rendered
with `fpdf2` and Noto Sans (OFL, `src/beluno/assets/fonts/`).

`GET /v1/plans/{id}/recap` sums up a plan for anyone who sees its money: days and
stops, people, spending in the base currency (the budget screen's numbers) per
person per day and by category, the most wanted place, itinerary progress, decided
polls, and who has settled (the ledger's own status and tolerance rule; people who left
with money still open are listed too). Its `share` object holds the only fields a public card
may show (route, start date and length, people, and the total if the person
chooses); the app draws the card, so the server serves no public link.

"Report a problem" posts to `POST /v1/support/reports`: a category, optionally a plan
and a record in it, the person's own words, and (by default) a diagnostic snapshot
with a code such as `BLN-7F3K-29QD` to quote to support. The snapshot holds IDs,
states, versions, sync sequences, and whether the plan's ledger reconciles; never
expense text, notes, names, or booking codes. `GET /v1/support/checks` shows the
same ledger check and whether the record belongs to the plan before sending.
Operators read reports with `uv run python scripts/support.py list --operator NAME` or
`scripts/support.py show BLN-XXXX-XXXX --operator NAME` (worker role); every report read
is audited with the operator's name, and the API itself reads none.

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

## Media

Receipts (on expenses that are not voided, trips and hangouts), trip covers, and trip
memories are files the app uploads
straight to S3-compatible storage (RustFS, self-hosted) through presigned URLs:
`POST /v1/plans/{id}/media` records the file (offline too, as sync command
`media.create`), `POST .../media/{media_id}/upload-url` signs a PUT for exactly the
declared type and size, and `POST .../uploaded` queues the worker, which checks the
real type and size, streams the bytes to ClamAV, rewrites images without any
metadata (EXIF, GPS; HEIC becomes JPEG), and stores the clean copy. Anyone in the
plan gets a five-minute download link (`POST .../download-url`; PDFs download as
attachments). A trip's
`cover_media_id` and `album_url` are set on the plan. Memories carry a caption, the
local day and time (sent by the app, since the server strips EXIF), and a saved
place; anyone on the trip shares them, their uploader or an organiser edits them
(`PUT .../media/{media_id}/memory`), and organisers pick up to 20 recap highlights
(`PUT .../highlight`), which the recap lists by day and time with the cover. Deleted files and purged plans
queue their objects, which the worker removes from storage. Configure
`BELUNO_STORAGE_*` and `BELUNO_CLAMD_HOST` (see `.env.example`).

## Notifications

Push notifications go through Firebase Cloud Messaging (`firebase-admin`; set
`BELUNO_FCM_SERVICE_ACCOUNT_JSON`, or they are recorded but not sent). Devices
register their token per session (`PUT /v1/me/push-token`); people choose categories
and quiet hours (`/v1/me/notification-settings`), nudge a task's assignee or someone
who owes them, and get a 21:00 summary of trips in progress. The database turns activity
(expenses and payments that involve you) and reminders (tasks due, polls closing)
into an outbox the worker delivers every minute. Messages carry localisation keys
for the app to render, never amounts, codes, or addresses: see
`docs/contracts/push-notifications.md`.

## Paid plans

A Trip Pass (per trip, for everyone on it) and Pro (yearly, for every trip its holder
owns) are bought in the App Store or Google Play and verified by the server: Apple's
signed transactions and notifications with `app-store-server-library`, Google's
purchase tokens with the Play Developer API and Pub/Sub push notifications. Free
accounts are limited to a number of their own trips in progress and receipts per trip
(`BELUNO_FREE_ACTIVE_TRIPS`, `BELUNO_MEDIA_RECEIPTS_PER_PLAN`; off until set); hangouts
are always free. See `docs/contracts/billing.md`.

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
