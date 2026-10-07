# Data Model

## Core Principles

- UUIDv7 IDs are client-generatable and sortable.
- PostgreSQL is the cross-device authority; client SQLite is a UI-local source.
- `amount_minor BIGINT` plus currency metadata represents money.
- Timed data stores UTC instants plus IANA timezones; date-only data uses SQL `date` and never fake midnight UTC.
- Mutable roots use optimistic versions and soft-delete tombstones.
- Every plan-scoped relationship is constrained by `plan_id` and stable `plan_participant_id` where applicable.

## Identity and Authorization

The `iam` schema holds users, sessions, and refresh-token digests:

- **users**: both registered and guest accounts; guests are upgraded in place when they claim an identity.
- **sessions**: one per device, with 90-day idle and 180-day absolute TTL; logout and device revocation are immediate.
- **refresh_tokens**: stored only as HMAC-SHA256 digests; single-use and rotation-guarded (theft detection).
- **email_challenges**: OTP codes and magic links for passwordless sign-in; delivery by Procrastinate worker.
- **user_identities**: links Google and Apple provider accounts; requires explicit account linking (no email-only matching).

Access tokens are 15-minute ES256 JWTs (/.well-known/jwks.json), signed with keys from `BELUNO_AUTH_SIGNING_KEYS`; every request re-checks session and account state in PostgreSQL.

## Plans and Participants

The `plans` schema models individual plans (trips or hangouts):

- **plans**: trips (destination-based, up to 10 stops) or hangouts (activity-based, optional icon). Each has a type (fixed at creation), base currency, state machine (draft → planning → active → settling → completed), and optional expected size.
- **plan_participants**: stable historical identities for votes, money, and RSVP; guest claims link to users without rewriting history. Members hold a default share (default 1.0×), a visible avatar color, and optional capabilities (`expenses.manage`, `budgets.manage`).
- **plan_invites**: bearer tokens for joining or claiming a placeholder; optional email binding, use limit, and guest switch.

## People and Crews

The `people` schema holds each user's private, saved lists of people:

- **crews**: a name and `member_user_ids` (1–50 registered users), owned by one registered user. A newly listed person must currently be active in a plan with the owner (`people.crew_write_guard` backs the API check). Owner-only under RLS; deletes are tombstones (`deleted_at`). Clients start new plans from a crew.

## Finance

The `finance` schema holds each plan's ledger (ADR 0003). Amounts are `BIGINT`
minor units of a currency pinned in `finance.currencies`.

- **plan_ledger_heads**: one per plan; `ledger_seq`, status (`open|settled|reopened`), open dispute count; every finance write locks it after the plan row.
- **ledger_accounts / account_balances**: one account per participant and currency plus a fund account per currency; balances are a synchronous projection.
- **expenses / expense_revisions / expense_payers / expense_splits**: stable identity plus immutable revisions with raw split input, `lr-v1` resolved shares, and an optional base-currency snapshot.
- **expense_refunds / refund_shares**, **settlements** (payments and waivers), **fund_settings / fund_movements**, **fx_snapshots**.
- **ledger_transactions / ledger_postings**: the journal; every transaction sums to zero per currency and carries the next `ledger_seq`.
- **budgets** and **cost_commitments** (finance-owned; other modules use the `CostCommitmentPort`).

Canonical rows are append-only (triggers reject UPDATE/DELETE); deferred
constraint triggers verify sums and posting shapes at commit; RLS limits every
row to active participants of its plan; the worker reaches finance data only
through SECURITY DEFINER reconciliation gates.

## Security and Audit

All table rows have RLS policies (SECURITY DEFINER helper functions):

- **RLS**: enforces tenant and participation boundaries; all writes pass through application authorization logic first.
- **Write guards**: tenant-aware triggers (000003_tenant_write_guards.py) prevent bulk operations and orphaned changes.
- **sync_audit.audit_events**: immutable log of user actions (created, modified, deleted), keyed by plan.
- **sync_audit.change_log**: pointer rows (`scope_type`, `scope_id`, `scope_seq`, entity, version, operation) with a contiguous per-scope sequence assigned by `sync_audit.append_changes` at commit; the source of cursor-based pull.
- **sync_audit.scope_heads**: per-scope last sequence, compaction floor, and generation (bumped after a restore).
- **sync_audit.operations**: stored outcomes of idempotent commands keyed by `(actor, command, idempotency_key)`, kept 180 days.
