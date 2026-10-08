# Receipt archive review (feat/receipt-archive, uncommitted)

Scope: migration 000023, modules/receipt_archives.py, api/routers/receipt_archives.py, storage.write_file/archive_key,
contracts, worker task, testkit pass_for, spreadsheet_text move, tests, docs. ~430 LOC added.

Gates: ruff OK, ruff format OK, mypy strict OK, OpenAPI export up to date and additive (no removals).
Pytest (live PG): **2 failed, 245 passed** -- `tests/security/test_idor_sweep.py::test_no_route_accepts_another_tenants_ids`
(`KeyError: 'archive_id'`) and `tests/security/test_rls_isolation.py::test_an_outsider_sees_and_changes_nothing_of_a_full_tenant`
(`media_memories.receipt_archives` neither filled by `full_tenant` nor listed in `NOT_TENANT_DATA`).

Probes (scratchpad, live PG) confirmed: voided expense receipts are packed; a row left in `building` is reused forever and
never rebuilt; each POST after `ready` makes a new full archive; plan purge cascades rows fine (zip stays until its +24h
deletion); a member who left gets 404; requester-only RLS holds. Six concurrent POSTs gave one archive (race not reproduced
through the ASGI client; still no DB guarantee).

## Critical

C1. Security suites red -- the new route and table are not in the tenant sweeps.
`tests/security/test_idor_sweep.py` (route param `archive_id` unknown) and `tests/security/test_rls_isolation.py:261`.
CI fails; the IDOR sweep never exercises GET `/receipt-archives/{archive_id}` with another tenant's ids.
Fix: make `full_tenant` (testkit) request (and build) an archive so the table is filled and swept, and add
`archive_id` to the tenant's id map. Do not add the table to `NOT_TENANT_DATA` -- it is tenant data.

## High

H1. A crashed/cancelled build leaves the row `building` forever and locks the requester out of that plan's archive.
`modules/receipt_archives.py:150-176` commits `state=building`, then builds outside any transaction; `retry=0`
(`worker/tasks.py:237`). Not covered by the `except Exception`: SIGKILL/OOM, deploy restart (`deploy/staging/compose.yaml:176`
`stop_grace_period: 30s` vs. a multi-minute 2 GiB build), `CancelledError` (BaseException), `storage_of()` 503 at :163
(outside the try), or `_finish` itself failing (DB blip). Then `request_archive` (:72-84) returns the stuck row on every POST,
and `build_archive` returns `skipped` (state != pending). Probe: set `building` -> POST returns same id `building`, build -> `skipped`.
Fix: (a) hourly sweep in `requeue_stuck_media`'s style marking `building`/`pending` rows older than N (e.g. 1 h) `failed`
(`storage`) -- also queue `archive_key(id)` for deletion since an upload may have landed; and/or (b) reuse only
`pending/building` rows with `created_at > now - N`. Add a test for each.

H2. Worker `/tmp` is tmpfs in the deploy target: the "on disk, never whole in memory" design holds up to 2 GiB per build in RAM.
`modules/receipt_archives.py:166` `tempfile.TemporaryDirectory()` -> `/tmp`; `deploy/staging/compose.yaml:22-25` mounts
`/tmp` as tmpfs for every process (worker included); worker runs `concurrency=4` (`worker/main.py:19`). Four large builds =
~8 GiB of RAM -> OOM kill of the worker, which also kills in-flight email/OTP and scan jobs and then triggers H1.
Fix: a dedicated setting (e.g. `BELUNO_RECEIPT_ARCHIVE_WORK_DIR`) pointing at a real volume, mounted in compose, passed as
`dir=`; cap concurrent builds per process (anyio `CapacityLimiter(1)` or a separate queue/worker) ; document disk sizing.
Consider a lower default cap than 2 GiB until a volume exists.

H3. Archives include receipts of voided expenses, listed as if they count. `alembic/versions/000023_receipt_archives.py:94-97`
joins `finance.expenses` without `e.state = 'active'`; voiding (`modules/finance/expenses.py:403-431`) keeps the media
`ready`. Probe: void the expense, build -> `receipts: 2`, CSV rows with amount `120.00` and no state column. An accounting
export that silently includes reversed spending is wrong data.
Fix: filter `e.state = 'active'` in `archive_entries` (new migration if 000023 has shipped anywhere; it has not -- edit in
place), or add a `voided` column to `receipts.csv`; decide with the user which. Add a test.

H4. Abuse/cost: nothing bounds archives per plan or reuses a ready one; the over-limit check downloads before failing.
`request_archive` (:72-84) reuses only `pending/building`; each POST after `ready` builds and stores a full new zip for 24 h
(probe: 3 POSTs -> 3 archives, 3 queued objects). Limit is the shared `EXPORTS_PER_USER` 20/h per user
(`modules/iam/rate_limits.py:42`), so one user = 20 x 2 GiB/h built and ~480 GiB-day held; every participant of an unlocked
trip can do the same, and 4 such builds occupy all worker slots shared with email (`WORKER_QUEUES`). Separately, `_pack`
(:195-198) only learns `too_large` after downloading up to 2 GiB.
Fix: return the latest `ready`, unexpired archive when no receipt was added/removed since its `created_at`
(or simply within e.g. 1 h); compute `sum(size_bytes)` from `entries` before any download and fail `too_large` immediately;
own rate limit (e.g. 5/day per user per plan); `queueing_lock=f"receipt-archive:{plan_id}"` or a per-plan cap.

## Medium

M1. Duplicate in-flight archives possible: check-then-insert (:72-110) with no constraint. Not reproduced through the ASGI
client, but two API processes can both insert. Fix: partial unique index on `(plan_id, requested_by_user_id) WHERE state IN
('pending','building')`, plain INSERT inside `begin_nested()`, on `IntegrityError` re-select (house pattern).

M2. Deleted receipts and purged plans stay in already-built zips for up to 24 h. Purge (`purge_deleted_plan`) cascades the
rows but leaves `archives/<id>.zip` queued at ready+24h (probe: `requested_at > now()` after purge); a receipt deleted by its
uploader stays downloadable in the requester's zip. Within the user's 24 h decision, but plan purge promises object removal.
Fix (cheap): in a migration, have the purge also move `archives/<id>.zip` keys of the purged plan to `now_at`; ask the user
whether receipt deletion should expire ready archives too.

M3. Tests miss the failure modes that matter: no `storage` failure path, no stale `building`, no voided expense, no
viewer/guest request, no left-member 404, no purge with an archive, no receipt deleted mid-build (`data is None` branch
:205-206 never runs), no hangout (free) path. `tests/integration/test_receipt_archives.py` covers only the happy path,
reuse, other-member 404, expiry, `NO_RECEIPTS`, `too_large`.

## Low

L1. `ObjectTooLarge` on a receipt (:203-206, e.g. after lowering `media_receipt_max_bytes`) is silently skipped; `receipts`
count drops with no trace in the CSV. Log it, or raise the read cap for archive reads to the stored `size_bytes`.
L2. `zip_.writestr` + CRC of up to 15 MiB per receipt run on the worker event loop shared with email/scan jobs; and each
receipt is read whole into memory. Acceptable at concurrency 4; `anyio.to_thread` for the write would remove loop stalls.
L3. `queue="media"` literal (:114) instead of `MEDIA_QUEUE`; `type: ignore[arg-type]` x2 in the router (:57-58) -- type
`ArchiveView.state`/`failure` as `Literal`s instead.
L4. Merged guests: an archive requested as a guest is invisible after merge (`requested_by_user_id = actor_id()`); 24 h, minor.
L5. CSV `amount` is the current revision's gross amount; refunds are not reflected. Document it in `docs/contracts/billing.md`.

Verified OK: no `RETURNING`/`ON CONFLICT` on the RLS table from the API (ORM flush, plain INSERT; `ON CONFLICT` only on
`object_deletions` from the worker, matching `media.py:71`); definer function revoked from PUBLIC, granted only to
`worker_runtime`; api has SELECT/INSERT only; RLS requires requester + active participant; no storage I/O inside
transactions (presign is local); zip names safe (`\w`-only slug, numbered, date prefix: no traversal, no duplicates, bidi/zero-width
stripped); CSV description escaped via `spreadsheet_text`, other columns are dates/codes/UUIDs/positive decimals;
deletion-queue key regex is anchored; account deletion is soft (`status=deleted`) so the `iam.users` FK does not block it;
plan purge FK cascade works (probed); audit row on request.

## Recommended order
1. C1 (CI), 2. H1 + H2 (stuck rows, RAM-backed temp), 3. H3 (data correctness), 4. H4/M1 (cost), 5. M2/M3.

## Unresolved questions
- Voided expenses: exclude from the archive, or include with a `voided` column? (accounting intent)
- Should deleting a receipt or purging a plan expire already-built archives immediately?
- What disk does the production worker get? A work-dir setting needs a volume in the deploy compose.
