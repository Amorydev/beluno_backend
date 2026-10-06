# ADR 0004: Offline Sync

**Status:** Accepted (expanded 2026-10-06)

Clients persist local changes and an outbox atomically. The server accepts
idempotent commands and exposes cursor-based changes from an append-only change
log. Realtime is only a wake-up hint; financial conflicts never use blind
last-write-wins.

## Decisions

- **Scopes.** Sync runs as independent streams per `user:{id}`, `group:{id}`,
  and `plan:{id}` scope, each with its own cursor. The handshake directory,
  recomputed from current relationships on every connect, is the revocation
  signal: a scope that disappears is purged locally.
- **Sequencing.** Every change row carries a `scope_seq` that is contiguous per
  scope, assigned under a row lock on `sync_audit.scope_heads` by the SECURITY
  DEFINER gate `sync_audit.append_changes` just before commit. Application code
  buffers audit and change records on the command context and never writes
  change rows directly, so a reader that has seen sequence N can never later
  see a smaller sequence appear.
- **Pointers, not payloads.** Change rows name an entity and version; pull
  renders the entity's current, authorization-filtered state and emits a
  delete for rows that are gone or no longer visible. Entities never embed
  other entities.
- **Cursors** are opaque HMAC-signed tokens bound to the user, scope,
  generation, access level, and position (sequence plus watermark, or
  snapshot progress). A floor, restore (generation bump), or access change
  yields `resync_required`.
- **Idempotency.** One command catalog serves REST and push. The scope
  `(actor, command, key)` is serialized with a transaction-scoped advisory
  lock; successful outcomes are stored with the domain write and replayed for
  identical repeats; a different payload under the same key is refused.
- **Conflicts.** Strict expected versions with the canonical snapshot on
  `412`; intent commands for RSVP and lifecycle answers; deletes win;
  fractional ordering keys for ordered collections.
- **Retention.** 90-day offline window; change rows, tombstones, and
  operation records kept at least 180 days; compaction raises per-scope
  floors and refuses cutoffs inside the offline window.
- **Jobs.** The Procrastinate job table is the transactional outbox
  (`defer_in_transaction`); failed jobs are the dead-letter queue with audited
  replay. A separate outbox table and dispatcher are deferred until a consumer
  needs fan-out.

## Consequences

- Finance and planning modules add commands to the catalog and record
  mutations through `record_mutation`; they inherit replay, conflicts,
  sequencing, and pull without new reliability code.
- Writes to one scope serialize on its head lock at commit; measure before
  sharding a hot plan.
- A database restore requires bumping every scope generation so devices
  bootstrap again (see `docs/runbooks/sync-operations.md`).
