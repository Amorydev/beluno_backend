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

## Groups and Plans

The `groups` and `plans` schemas model social structure:

- **groups**: reusable containers; members, owners, admins; deletion is scheduled, not immediate.
- **group_invites**: reusable tokens with optional use limits; grant new members a role.
- **plan_series**: recurring dinner, coffee, sport, trip, etc.; generates instances on a schedule.
- **plans**: individual instances from series or standalone; support state machines (draft → planning → active → settling → completed).
- **plan_participants**: stable historical identities for voting, money, and task ownership; guest claims link to users without rewriting history.

## Security and Audit

All table rows have RLS policies (SECURITY DEFINER helper functions):

- **RLS**: enforces tenant and participation boundaries; all writes pass through application authorization logic first.
- **Write guards**: tenant-aware triggers (000003_tenant_write_guards.py) prevent bulk operations and orphaned changes.
- **sync_audit.audit_events**: immutable log of user actions (created, modified, deleted), keyed by plan or group.
- **sync_audit.change_log**: pointer rows (`scope_type`, `scope_id`, `scope_seq`, entity, version, operation) with a contiguous per-scope sequence assigned by `sync_audit.append_changes` at commit; the source of cursor-based pull.
- **sync_audit.scope_heads**: per-scope last sequence, compaction floor, and generation (bumped after a restore).
- **sync_audit.operations**: stored outcomes of idempotent commands keyed by `(actor, command, idempotency_key)`, kept 180 days.
