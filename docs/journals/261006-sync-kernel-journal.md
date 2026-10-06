# 2026-10-06 — Reliability and sync kernel

## What changed

- Change rows are buffered on the command context and written at commit through `sync_audit.append_changes`, which locks scope heads in sorted order and assigns contiguous per-scope sequences; runtime roles lost direct INSERT on `change_log`.
- One command catalog (33 commands) serves REST and `POST /v1/sync/push`: bounded deadlock/serialization retries, optional `Idempotency-Key` (replayed outcomes, `409 IDEMPOTENCY_KEY_REUSED`), `412 VERSION_CONFLICT` with the current snapshot, client-generated IDs for segments, series, and participants.
- `POST /v1/sync/handshake` (protocol window, directory of scopes with head/floor/generation/access) and `POST /v1/sync/pull` (snapshot bootstrap, high-watermark change pages, HMAC cursors bound to user/scope/generation/access level, `unavailable`/`resync_required`).
- User-scope `plan_access`/`group_access` signals, member-name refresh, fractional ordering keys, retention jobs, dead-letter tooling, OpenTelemetry metrics, runbook.

## Lessons

- A model-based test paid for itself before merge: feed entities that embedded other entities (`plan.my_participant`, `travel_details.segments`) made incremental views drift from bootstraps. Entities now never embed other entities.
- `SELECT ... FOR UPDATE` on a SECURITY DEFINER-managed head row at the very end of the transaction keeps head locks short and cycle-free; locking heads earlier would have deadlocked against domain row locks.
- Hypothesis plus a real database works when the example resets the database itself and the test stays synchronous (`asyncio.run` per example); keep examples few and derandomized.
- Rate-limit assertions must measure deltas: sign-in already counts a hit.
- Procrastinate's status trigger resolves its enum types through `search_path`; any direct SQL on its tables must set it.

## Decisions taken with the user

Per-scope streams and cursors; pointer change rows; HMAC cursors; optional `Idempotency-Key` on REST; strict versions with `current`; 90/180-day retention enabled from the start; the job table stays the outbox; per-scope ordering after transient push failures; metrics plus a runbook instead of provisioned dashboards.

## Open

Realtime hints; finance revisions (Phase 5); itinerary ordering adoption (Phase 6); staging SLO and dashboards (Phase 8); `change_log` partitioning once volume is known; GitHub App access for pushing from cloud sessions.
