# Sync Protocol

PostgreSQL is the cross-device authority. Clients keep a durable outbox of
operations and a local copy of each scope they can see; the server accepts
idempotent commands through push (or REST) and serves authorization-filtered
changes through cursor-based pull. Realtime notifications are only a wake-up
hint; the handshake, push, and pull endpoints are the durable protocol. Server
sequence numbers order changes; client timestamps are untrusted metadata.

Protocol version window: `1..1`. A client outside the window receives
`426 CLIENT_UPGRADE_REQUIRED` from every sync endpoint.

## Scopes

A scope is one independent change stream with its own cursor:

| Scope | Entities (`entity_type`) | Who may read it |
|---|---|---|
| `user:{id}` | `user`, `session`, `plan_access`, `crew`, `activity_event` (user scope) | the user (`self`) |
| `plan:{id}` | `plan`, `plan_participant`, `plan_invite` (managers), `activity_event` (plan scope); finance: `ledger`, `expense`, `settlement`, `budget`, `cost_commitment`, `fund`, `fund_movement`, `fund_count`, `consolidation`; planning (trips): `place`, `itinerary_item`, `poll` | active participants (`manager` = owner/admin, `member` = everyone else); pending participants and invites are shown to managers only |

Entity payloads are the REST representations with one exception: the `plan`
entity has no `my_participant` (use the caller's `plan_participant` row).
Entities never embed other entities. Invites carry no token. Finance values
that are not independent entities travel inside their owner: an `expense`
carries its current immutable revision (payers, split input, resolved shares,
base-currency snapshot and the base-change number it was valued at, occurred
time and zone, revision origin) and its refunds; the single `ledger` entity per
plan (`entity_id` = plan id) carries the ledger status, sequence, open dispute
count, money settings, every account balance per currency, the confirmations
at the current sequence, and every base-currency change (the chain clients read
base values through), and `suggestions` (the current deterministic settlement
suggestions per currency, never netted across plans or currencies, same computation
as `GET /ledger/settlement-preview`). A `consolidation` carries its frozen rates and lines.
Revision history and the journal are REST-only (`/expenses/{id}/revisions`, `/ledger/transactions`). `plan_access`
is a user-scope signal (`PlanAccessSignal`) describing the caller's own
participation; a non-active state means the matching scope is no longer theirs
unless the directory still lists it.

## Handshake: `POST /v1/sync/handshake`

Request: `protocol_version`, optional `client` metadata, optional
`directory_cursor` for the next page of scopes.

Response: the protocol window, feature flags (`push_enabled`, `pull_enabled`,
`disabled_commands`, `finance_writes_enabled`), limits, retention, the command catalog (`name`,
`schema_version`, `versioned`, `target_fields`), and the directory: every
scope the caller may read now with `head`, `floor`, `generation`, and `access`
level. The user scope comes first; pages continue with `next_directory_cursor`.

Client rules:

- Call the handshake on every connect. A scope missing from the directory is
  revoked: purge its local data and outbox entries that target it.
- A scope whose `head` moved past your stored position has changes to pull.
- A scope whose `generation` or `access` differs from the one your cursor was
  issued under will answer `resync_required`; bootstrap it again.

## Push: `POST /v1/sync/push`

Request: `protocol_version`, optional `device_id`, and up to 100 operations
(configurable, at most 1 MiB per batch) in the client's order:

```text
operation_id          UUID; also the idempotency key for this actor and command
command               catalog name, e.g. plan.update
schema_version        command schema version (currently 1)
target                IDs named by the command's target_fields, e.g. {"plan_id": ...}
expected_version      required for versioned commands (If-Match equivalent)
depends_on            operation IDs that must have applied first (this batch or earlier)
payload               the command's request body (same contract as REST)
client_created_at     metadata only
```

Every operation runs in its own transaction and gets its own result:

| Outcome | Meaning | Client action |
|---|---|---|
| `applied` | executed now; `status`, `version`, `body` carry the response | remove from outbox, apply `body` locally |
| `replayed` | identical operation was already applied; stored response returned | same as applied |
| `conflict` | `409` state conflict or `412 VERSION_CONFLICT` with `problem.current` | permanent for this payload; reconcile and resend with a new `operation_id` |
| `rejected` | validation, authorization, not-found, or missing `expected_version` | permanent; drop or fix |
| `upgrade_required` | command or protocol version unsupported | keep the outbox, upgrade the client |
| `retry` | rate limit (`retry_after_seconds`), disabled feature, transient database conflict | keep, retry unchanged later |
| `skipped` | not attempted: an earlier operation on the same plan must be retried first, or a dependency was not applied | keep (or drop with the failed dependency) |

Order is preserved: after a `retry`, later operations on the same scope are
`skipped` so they cannot overtake. A permanent failure only skips operations
that declared it in `depends_on`. Replays never count against rate limits. A
revoked session fails the whole request with `401`; a batch-level limit fails
it with `429` and `Retry-After` (keep the queue). Retry with exponential backoff
and jitter, keeping `operation_id` and payload unchanged.

REST mutations accept the same semantics through the optional
`Idempotency-Key` header (same key and body → stored response plus
`Idempotency-Replayed: true`; same key and different body → `409
IDEMPOTENCY_KEY_REUSED`). Credential-issuing endpoints (sign-in, refresh,
invite preview/redeem, invite creation and rotation) are not idempotent
commands. Stored outcomes are kept for 180 days.

## Pull: `POST /v1/sync/pull`

Request: `protocol_version`, up to 20 `{scope, cursor}` pairs, optional
`page_size` (default 200, max 500).

Per-scope response: `status`, `cursor`, `has_more`, `head`, `changes`.

- `cursor: null` bootstraps the scope: the server captures the head, streams
  a snapshot of current rows (`seq: null`) entity type by entity type, then
  continues as a change feed from the captured head. Nothing that happens
  during the snapshot is lost.
- A change page covers `(cursor, watermark]` where the watermark is the head
  captured on the first page of a multi-page pull; changes after it belong to
  the next pull. Repeated entities inside a page collapse into the latest one.
- Each item: `seq`, `entity_type`, `entity_id`, `operation` (`upsert` or
  `delete`), `version`, `changed_at`, `data` (null for deletes). A delete is
  also emitted when the row no longer exists or the caller may no longer see it.
- `status`: `ok`; `unavailable` (the scope is not the caller's: purge it);
  `resync_required` (cursor below the compaction floor, after a restore, from
  another generation or access level, forged, or from another account: pull
  again with `cursor: null`). A cursor presented for a different scope is a
  `422`.

Client apply rule: apply the whole page and store its cursor in one local
transaction; keep the old cursor if applying fails (replaying a page is safe:
the same sequences return with data at least as new). Apply an `upsert` only
when its `version` is greater than or equal to the local version. A `delete`
carries the version at which the row disappeared: keep it as a tombstone, drop
upserts whose `version` is not greater than it, and let an upsert with a
greater version revive the entity. Sessions are never revived. Keep pending local edits; conflicts surface
when they are pushed.

Cursors are opaque, HMAC-signed tokens bound to the user, scope, generation,
access level, and position. Never edit or share them.

## Conflict policy

| Entity / action | Policy |
|---|---|
| Versioned entities (plan, participant settings, profile, crew) | strict `expected_version`; `412` returns `current`; no server-side last-write-wins |
| Intent commands (RSVP, leave, join-request review, removal) | no version; the server applies the intent to current state |
| Delete versus edit | the delete wins; recreate with a new ID |
| Ordered collections (future itinerary) | fractional ordering keys from `beluno.sync.ordering`; deterministic rebalance |
| Expenses (revise, void, refund) | strict `expected_version` on the expense; a revision appends a reversal and a new revision, never an overwrite; a voided expense rejects further changes (`409`); a refunded expense keeps its currency and split (`409 INVALID_STATE_TRANSITION`) |
| Settlements | recording is a create; `confirm`/`dispute` are creditor intents; `reverse` needs `expected_version` and appends exact reversals |
| Budgets, commitments, fund settings | strict `expected_version`; budget delete is an intent (delete wins) |
| Fund movements, waivers, adjustments | creates only; corrections are new entries |

## Retention

Offline window 90 days; change rows, tombstones, and operation records are
kept at least 180 days (`docs/contracts/retention-matrix.md`). Compaction
raises a scope's `floor`; a cursor below it must bootstrap.
