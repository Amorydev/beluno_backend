---
phase: 4
title: "Reliability and Sync Kernel"
status: pending
priority: P1
effort: "4 weeks (2 backend engineers + client-contract review)"
dependencies: [3]
---

# Phase 4: Reliability and Sync Kernel

## Context Links

- [Plan overview](./plan.md)
- [Phase 3: Identity, Groups and Plans](./phase-03-identity-groups-plans.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- HTTP `Retry-After`: <https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after>

## Overview

Build the cross-domain reliability kernel before finance and planning modules fan out: transactional idempotency, optimistic concurrency, append-only audit/change records, tombstones, durable push/pull sync, transactional jobs/outboxes, reconciliation primitives, and fault-test tooling. Realtime is only a wake-up hint; `change_log` and cursors are the durable protocol.

## Requirements

### Functional

- Accept client-generated operation IDs, device IDs, schema versions, expected entity versions, dependencies, and idempotency keys.
- Process push batches in dependency/order per plan while returning an independent result for every command.
- Pull deterministic pages after a cursor using a captured high watermark; expose compaction floor and full-resync contract.
- Record audit, change log, outbox/job, and idempotent canonical response in the same transaction as each domain write.
- Support tombstones, participant-access revocation signals, schema/version handshake, and plan/account snapshot bootstrap.
- Provide entity conflict policies: finance explicit revision; intent commands for toggles; vote upsert; fractional ordering; tombstone wins.

### Non-functional

- A lost response after commit must be safely replayable without duplicate state or side effects.
- Server sequence, not client time, defines change order. Client timestamps remain untrusted metadata.
- Idempotency retention and tombstone retention exceed the supported offline window; proposed 180 days for a 90-day window.
- Batch limits start at 100 operations or 1 MiB and are configurable; permanent failure of one operation must not retry unrelated operations.
- No arbitrary sleeps in tests: use deterministic clocks, seeded generators, barriers, and injectable fault points.
- All protocol versions and stable error codes are represented in OpenAPI/contract fixtures.

## Architecture

### Operation envelope

```text
operation_id, idempotency_key, actor_id, device_id, plan_id,
command_type, command_schema_version, entity_id, expected_entity_version,
dependency_operation_ids, payload, client_created_at
```

The server computes a canonical request hash. Unique scope is `(actor_id, endpoint_scope, idempotency_key)`. Same key/same hash returns the stored status/body/resource/version; same key/different hash returns `409 IDEMPOTENCY_KEY_REUSED`. The idempotency row and domain state commit together.

### Change envelope and pull

```text
server_seq, scope_type, scope_id, entity_type, entity_id,
operation, entity_version, changed_at, payload_or_pointer, schema_version
```

At first pull page, capture `high_watermark`. Page by `(server_seq)` until the watermark, then return the next cursor. Changes after it belong to the next pull. Cursor encodes scope, sequence, protocol generation, and integrity protection; it must not be a raw user-editable offset.

### Transaction pattern

```text
BEGIN
  reserve/replay idempotency record
  set local actor context; authorize current state
  lock aggregate head or validate version
  execute domain command
  append audit_event + change_log + domain/notification outbox/job
  store canonical operation response
COMMIT
```

Procrastinate executes PostgreSQL-backed jobs. Domain transactions still write an application outbox atomically; a dispatcher defers jobs idempotently, and handlers remain at-least-once and independently idempotent. Provider delivery tables retain dedup keys and attempt history.

### Conflict matrix

| Entity/action | Policy |
|---|---|
| Expense/split/FX/settlement | Reject stale version; return canonical snapshot/conflict; resolve via new revision |
| Plan title/cover/low-risk notes | Audited LWW only if ADR permits; otherwise version conflict |
| Checklist/task status | Intent command (`mark_done`, `reopen`) plus version |
| Poll vote | Upsert by voter/option/rule with uniqueness |
| Schedule ordering | Fractional key; deterministic server rebalance operation |
| Attachment | Append-only object; metadata versioned separately |
| Delete versus edit | Tombstone wins; recreate uses a new ID |

### Snapshot and compaction

Initial/full resync returns an authorization-filtered snapshot plus a cursor. Compaction may remove old payloads only after retention and backup gates; tombstones and financial/audit history obey longer policies. Cursor before compaction floor returns `410 FULL_RESYNC_REQUIRED`. Pending outbox migration/quarantine is a client responsibility defined in the handshake contract, never silent loss.

## Proposed File Inventory

All entries are `[UNVERIFIED]` proposed paths.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/src/beluno/sync/{protocol,push_service,pull_service,idempotency_service,conflicts}.py` `[UNVERIFIED]` | Sync contracts, dispatch and conflicts | Contract/model |
| Create | `backend/src/beluno/modules/sync_audit/*` `[UNVERIFIED]` | Audit/change/outbox persistence | Integration |
| Create | `backend/src/beluno/db/transaction.py` `[UNVERIFIED]` | AsyncSession transaction hooks/context/retry | Concurrency |
| Create | `backend/alembic/versions/000020_reliability_sync.py` `[UNVERIFIED]` | Operations, change log, tombstones, audit, outboxes, heads | Migration |
| Create | `backend/src/beluno/worker/tasks/reliability/*` `[UNVERIFIED]` | Compaction/reconciliation/dead-job handling | Worker |
| Create | `backend/tests/sync/test_model.py` `[UNVERIFIED]` | Hypothesis multi-device state machine | Sync gate |
| Create | `backend/tests/integration/test_idempotency.py` `[UNVERIFIED]` | Commit/lost-response replay | Integration |
| Create | `backend/tests/integration/test_change_log.py` `[UNVERIFIED]` | Paging/high watermark/security | Integration |
| Create | `backend/tests/contract/sync-fixtures/*` `[UNVERIFIED]` | Client/server compatibility corpus | Contract |

Phase 2 transaction/OpenAPI/testkit and Phase 3 mutation paths will be modified after actual paths are verified. Nothing is deleted.

## Implementation Steps

1. Finalize protocol/error/version registry and generate client-consumable schemas for handshake, push item result, conflict, pull page, snapshot, and full-resync response.
2. Add operation/idempotency tables with unique scopes, canonical hashes/responses, expiry, status, and lookup indexes.
3. Add per-scope monotonic change sequencing, audit events, tombstones, aggregate heads, outbox/delivery tables, and compaction metadata.
4. Implement one transaction wrapper that sets local actor context, performs bounded SQLSTATE `40001`/deadlock retries, and never retries validation/authorization errors.
5. Implement a domain command registry; unsupported/old schema versions return `CLIENT_UPGRADE_REQUIRED` or a documented compatibility transform.
6. Implement push validation, dependency ordering, per-plan serialization, independent item transactions/results, body/count limits, and `Retry-After` responses.
7. Implement canonical idempotent replay including kill-after-commit fault injection and same-key/different-payload collision response.
8. Implement pull with authorization scope, high watermark, stable page ordering, cursor integrity, minimal payload/pointer policy, and cursor advancement contract.
9. Implement snapshot/full-resync generation and compaction-floor checks. Verify removed participant data is omitted and revocation/purge markers are delivered safely.
10. Implement conflict policies and deterministic fractional-order rebalance command; ban generic blind LWW for financial resources.
11. Implement transactional job/outbox APIs, handler dedup/retry/backoff/DLQ metadata, replay tooling, and oldest-age metrics.
12. Instrument operation latency/result, replay/collision, conflicts, serialization retries, pull lag/bytes, cursor age, full resync, outbox age, and permanent failures.
13. Retrofit Phase 3 mutations to write audit/change/idempotency atomically; verify no accepted state change is missing from pull.
14. Build model-based multi-device tests that duplicate/drop/reorder requests/responses/pages and exercise 30/90-day offline, stale schema, wrong clocks, deletes, and permission revocation.
15. Document client apply rule: apply a whole page and advance cursor in one local transaction; preserve pending edits; classify retryable/conflict/auth/upgrade/permanent outcomes.

## Todo

- [ ] Protocol handshake, envelopes, errors, and version window are frozen.
- [ ] Idempotency response replay is atomic with domain writes.
- [ ] Push isolates item failures and preserves per-plan dependencies.
- [ ] Pull uses high-watermark pages and integrity-protected cursors.
- [ ] Snapshot, tombstone, compaction floor, and full-resync flows work.
- [ ] Conflict policy is implemented for every Phase 3 and planned domain entity.
- [ ] Audit/change/outbox records are transactionally complete.
- [ ] Worker retry, dedup, DLQ, replay, and metrics are operational.
- [ ] 90-day offline and client upgrade fixtures pass.
- [ ] Multi-device fault model converges without acknowledged-write loss.

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | API commits then connection drops before response | Replay returns identical canonical response; one domain mutation/change only |
| Critical | Same idempotency key with different payload | `409 IDEMPOTENCY_KEY_REUSED`; original state unchanged |
| Critical | Pull page apply fails locally | Client contract keeps old cursor; replay page is deterministic |
| Critical | Removed participant pushes queued mutation | Authorization failure, no state change; purge/access-revoked signal available |
| Critical | Financial entity edited from two stale devices | One accepted revision; other gets explicit canonical conflict |
| High | One item in 100-operation batch is invalid | Other independent items complete; invalid item permanent-fails alone |
| High | Changes arrive during paginated pull | Current pages stop at captured high watermark; later pull receives new changes |
| High | Cursor is older than compaction floor | `410 FULL_RESYNC_REQUIRED` with recovery metadata |
| High | Worker crashes after provider accepts | Retry does not create an unintended duplicate beyond defined provider limits |
| Medium | Realtime notification lost/reordered | Later cursor pull converges completely |
| Medium | Device clock is months wrong | Server order/version remains correct; client time retained only as metadata |

## Success Criteria

- [ ] Fault corpus proves no duplicated domain mutation or lost acknowledged write.
- [ ] Every Phase 3 mutation produces exactly one auditable change visible to authorized pull.
- [ ] Push/pull contract handles partial success, conflicts, revocation, old clients, and full resync without ambiguity.
- [ ] Model-based 2–3 device tests converge from duplicate/drop/reorder/lost-ack sequences with reproducible seeds.
- [ ] P95 sync-after-connect target and batch limits are measured on realistic fixtures; final SLO is recorded.
- [ ] Job/outbox dashboards expose oldest age, retries, permanent failures, DLQ depth, and replay actions.
- [ ] Finance and planning teams can implement domain commands without inventing new reliability semantics.

## Risk Assessment

- **Custom sync complexity:** protocol bugs can corrupt trust. Keep the command set explicit, preserve a compatibility corpus, and model-test end states.
- **Unbounded change log:** payloads/retention can grow rapidly. Use minimal payloads, partition/index from measured volume, snapshot/compaction policy, and quotas.
- **Hot plan/ledger head:** large groups can serialize. Measure contention; shard only after evidence, never weaken finance ordering first.
- **Idempotency storage pressure:** 180-day records are large. Store normalized response/pointers, partition/archive safely, and never shorten below offline guarantees.
- **Worker duplicate side effects:** at-least-once is unavoidable. Require delivery dedup keys and provider reconciliation.

## Security Considerations

- Authorize every push item and pull page against current membership; a valid cursor or operation ID grants no access.
- Sign/encrypt cursors and validate actor/scope binding; reject scope substitution.
- Redact operation payloads in logs and limit audit payloads by data class.
- Rate limit by IP/account/device/plan/endpoint while avoiding shared-IP lockouts; return `Retry-After` without discarding queued operations.
- Restrict DLQ/replay/support tools, record every action, and prevent replay under a removed actor's former authority.

## Rollback and Exit Gate

Protocol/database changes use additive generations. Keep the prior pull/push generation available through the published compatibility window; disable new command types with a kill switch rather than deleting accepted operations. Never truncate `change_log`, idempotency, or tombstones as rollback. Exit requires lost-response proof, convergence fault tests, 90-day/full-resync fixtures, atomic audit/outbox coverage, and production dashboards before Phases 5 and 6 start.
