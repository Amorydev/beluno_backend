# Code review: trip planning slice 1 (places + itinerary)

Branch `feat/planning-itinerary-places`, uncommitted tree, reviewed 2026-10-07.

## Scope
- Files: migration `000011_planning_places`, `db/models/schedule_places.py`, `modules/planning/*`, `contracts/planning.py`, `api/{commands/planning.py,routers/planning.py,planning_projection.py,projection.py}`, `authorization/policy.py`, `modules/finance/commitments.py`, `modules/activity/events.py`, testkit, tests, docs, OpenAPI.
- LOC: ~2.3k new (src+tests), plus doc/OpenAPI diffs.
- Probes (scratchpad only, not committed): HTTP probes through the real app + insider SQL probes as `api_runtime`; text-ordering check on `postgres:16` vs `postgres:16-alpine` containers.

## Gates run
| Gate | Result |
|---|---|
| `ruff check .` | pass |
| `ruff format --check .` | pass |
| `mypy src` (strict) | pass |
| new tests + `tests/security` + `test_plan_purge.py` + `test_finance_budgets.py` | 255 passed |
| full suite under coverage | 569 passed, total 96% (planning modules 76–100%) |
| OpenAPI snapshot vs `create_app().openapi()` | in sync; compat checker exit 0; no removed/changed schemas or paths (purely additive) |

## Overall assessment
Structure follows the house patterns (open_context, load_plan + policy, record_mutation, savepoint inserts, no RETURNING/ON CONFLICT, plan-row-first lock order). DB guards hold under insider probing. But the core edit path for any itinerary item that carries an estimated cost is broken (500), and two latent data issues (fractional-key collation, orphaned estimates) will corrupt ordering/budgets in production. Not ready to merge.

## Critical

### C1. Editing an item with a live estimated cost always 500s (guard violation)
- `src/beluno/modules/planning/itinerary.py:142-151` — `update_item` mutates the item in `_apply` (title, day, status, order_key…), then calls `_record_cost` **before** `_bump` raises `version`. Both port paths end in `commitments._bump` → `session.flush()` (`src/beluno/modules/finance/commitments.py:169` via `record`, `:192` via `cancel`), which flushes the pending item UPDATE with the old version; `schedule_places.guard_root` rejects it (`NEW.version <> OLD.version + 1`).
- Probe (real app): item with `estimated_cost`; PUT with changed title + same cost → `ProgrammingError InsufficientPrivilege` (500). PUT with changed title and no cost → same 500. Also hits: moving a costed item to another day, marking it done, cancelling it while the cost is still `estimated`. Only "no field changed" PUTs and items whose commitment is already converted (cancel early-returns, no flush) survive — which is exactly what the tests cover, so CI is green.
- Fix: bump/flush the item first, or record the cost before mutating the item. E.g. in `update_item`: `await _apply(...)`; `entry.version += 1; entry.updated_at = ctx.now; await ctx.session.flush()`; then `_record_cost(...)`; then `_record(...)` (split `_bump`). Or call `_record_cost` before `_apply` passing `draft.title` as the description. Add an integration test: costed item → rename, move day, mark done, cancel (estimated, not converted).

## High

### H1. Fractional `order_key` ordering depends on the database collation
- `alembic/versions/000011_planning_places.py:93` (`order_key text`, default collation), used by `itinerary.py:85` (`ORDER BY order_key`) and `itinerary.py:290-295` (`max(order_key)`); index `itinerary_items_day_idx` also collation-ordered.
- Keys are designed for byte order (`0-9 < A-Z < a-z`, `sync/ordering.py:15`). Verified: on `postgres:16` (Debian, glibc `en_US.utf8`) `ORDER BY` gives `0V,a,B,k,V` and `max('V','k') = 'V'`; on `postgres:16-alpine` and the local test cluster (C) it is byte order. The test cluster is `C`, so tests cannot see it. On a glibc/ICU-collated production DB: list order is wrong, and `_last_key` picks the wrong max → appended keys collide with or sort before existing ones (e.g. `max='V'` → new key `'k'`, duplicating an existing `'k'`). Clients sorting by byte order disagree with the server.
- Fix (cheap now, table is new): `order_key text COLLATE "C" NOT NULL ...` in 000011 (and keep the index on it). Add a test that orders mixed-case keys (`'V'`, `'a'`, `'k'`).

### H2. A deleted/cancelled item's cost resurrects as a permanent estimate when its expense is voided
- `commitments.py:189-190` (cancel leaves converted as-is — the user decision) + `commitments.py:240-249` (`release_expense` restores `converted_from_state`).
- Probe: item with cost → expense links commitment → delete item (204, commitment stays converted) → void expense (200) → commitment back to `estimated`, budget overview `estimated_minor: 4000`, `projected_minor: 4000`. The source item is tombstoned, `PUT /commitments/{id}` returns 403 ("Change this cost where it was planned"), so nothing can remove it. Same for a cancelled item, except there the user can re-save the item to cancel again.
- This does not reverse the user decision (expense stays); it is a gap the decision did not cover. Options: (a) when `cancel()` meets a converted commitment, record that the source withdrew it (e.g. `converted_from_state = 'cancelled'`, needs a migration widening the CHECK at `000005:161,177`; or a `source_withdrawn_at` column) so `release_expense` restores `cancelled`; (b) let `update_manual` cancel non-manual commitments whose source no longer plans them; (c) accept and document. Recommend (a); ask the user.

### H3. `maps_url` accepts any scheme and is stored and returned verbatim
- `src/beluno/contracts/planning.py:24` (`MapsUrl` = length only), `src/beluno/modules/planning/places.py:202` stores it.
- Probe: `javascript:alert(document.cookie)` → 201, `maps_url` echoed, `resolution_state: pending`; `intent://…` → 201. Every participant's client receives it via REST and sync and the UI will render "Open in Maps". Any contributor (guests included) can plant a `javascript:`/`intent:`/custom-scheme link for others to tap.
- Fix: at the boundary, require `http`/`https` (and a hostname) — e.g. an `AfterValidator` on `MapsUrl` using `urlsplit`, 422 otherwise. Add the cases to tests.

## Medium

### M1. Unhandled exceptions → 500 on user input
- `maps_links.py:76-81`: `mlat=NaN` → `Decimal('NaN').quantize` succeeds, then `-90 <= NaN` raises `InvalidOperation` outside the `try` → 500 (probe confirmed). Fix: reject `not value.is_finite()` inside the try, or wrap the range check.
- `contracts/planning.py:68` `start_time: time` accepts offsets; `"10:00:00+02:00"` → `resolve_local` raises `ValueError("local time must be naive")` (`itinerary.py:226`) → 500 (probe). Fix: validator rejecting `tzinfo`, in both `ItineraryItemRequest` and `AddPlaceToPlanRequest`.
- `itinerary.py:295`: a contributor may set `order_key` to 128 `z`s (pattern allows it); every later append on that day raises `OrderingKeyError` → 500 for everyone (probe: poison 201, next append 500). Fix: catch `OrderingKeyError` and rebalance the day (or 409 with a code), and/or cap client keys below `MAX_KEY_LENGTH` to leave headroom.

### M2. Every item save rewrites the commitment and takes the ledger lock, even when the cost is unchanged
- `itinerary.py:143-144` → `CostCommitmentPort.record` always `_apply` + `_bump` (`commitments.py:162-170`). Probe: same-cost PUT bumped commitment v1→v2 → extra `finance.commitment_updated` audit + change_log + sync row per reorder/rename, plus `ledger_for` loading all participants/accounts/balances under the ledger-head lock. Also: omitting `category` resets it to `activities` (`contracts/planning.py:58`).
- Fix: in `_record_cost`, read the live commitment and skip `record()` when currency/amount/category/description are unchanged (also fixes M3 for the common case).

### M3. Finance kill switch blocks plain planning edits
- `itinerary.py:250-268`: with `finance_writes_enabled=False`, any PUT of an item that has (or sends) a cost → 503 (probe: marking done → 503), and deleting an item whose commitment is already *converted* → 503 although `cancel()` would be a no-op (`live_for_sources` counts converted rows). Fix: only raise `feature_disabled` when a finance row would actually change (unchanged cost → skip; converted → skip).

### M4. Authorship check ignores merged guest accounts
- `src/beluno/modules/planning/common.py:45` compares `author_id != actor.user_id`. Finance uses `beluno.modules.iam.users.is_actor_account` (handles `merged_into_user_id`, migration 000006). A guest who claims an existing account loses edit rights over their own places/items. Fix: `if not await is_actor_account(ctx, author_id)` (make the helper async).

### M5. Itinerary costs bypass the finance write rate limit
- `api/commands/planning.py:164-181`: no `rate_limit`; finance commands use `FINANCE_WRITES_PER_PLAN` per plan. Any contributor (guests included) can create unbounded commitments (each takes the ledger-head lock). Fix: apply `FINANCE_WRITES_PER_PLAN` with `rate_limit_target="plan_id"` to `itinerary.create`/`itinerary.update`/`itinerary.delete`, or add a planning-specific limit.

### M6. Test gaps (why C1, H1, H3, M1 shipped green)
- No update of an item whose cost is still `estimated` (C1). No ordering test with mixed-case keys, and the cluster is `C` (H1). No void-after-delete budget test (H2). The "count once" test checks commitment states only, never the budget overview totals. No guard tests for version skip / identity change / tombstone edit / editing someone else's reaction (only the attendance INSERT case). No tests for deletion-scheduled or completed plans, guest contribution, or author-vs-manager on items (only on places). Finance switch tested on create only. No DST-ambiguous, tz-aware, NaN, scheme, or long-key cases.

## Low
- L1. Migration header says "new objects only… no table lock". Creating FKs takes `SHARE ROW EXCLUSIVE` on `plans.plans`, `iam.users`, `plans.plan_participants` until commit — brief, but say so.
- L2. Insider (any active participant, viewers included) can INSERT places/items with forged `saved_by_user_id`/`created_by_user_id`, any `version`, any status, and UPDATE anyone's item content (probe confirmed). This matches the finance RLS pattern (no role in policies, UPDATE-only guards), so it is not a regression; an INSERT guard pinning author = actor and `version = 1` is cheap.
- L3. `guard_root` uses `to_jsonb(NEW) ->> col` four times per row. It is sound (uuid → text compare, missing key → NULL on both sides), but two small functions or a `TG_TABLE_NAME` branch would be clearer and cheaper.
- L4. `maps_links.py:17` matches `google.com.evil` as provider `google`. `AT_PAIR` has no trailing boundary: `@1.5,1234.5` parses to longitude `123`. Google `@lat,lng` is the viewport centre, not the pin (`!3d…!4d…`). No network, so no SSRF risk; it only affects coordinate quality.
- L5. `AddPlaceToPlanRequest` accepts `timezone` without `start_time` and drops it silently. `ItineraryItemRequest` refuses the same input (`contracts/planning.py:86` vs `:111-115`).
- L6. `wanted_by` and `attendance` include participants who were removed or left. "3 of 6 want to go" can count people no longer on the trip.
- L7. `add-to-plan` emits both `itinerary.item_added` and `place.added_to_plan`, so the feed shows two rows. A place in `poll_winner` gets no `added_to_plan` event. `item_added`/`item_done` have no `item_id`, so the feed cannot deep-link.
- L8. A place stays `in_plan` after its only item is deleted (the test asserts this). Confirm that is the intended product behaviour.
- L9. Trips created without a timezone (the default in `finance_plan`) make "defaults to the plan's timezone" a 422 for every timed item. The message is clear, but clients must always send `timezone`.
- L10. The OpenAPI diff shows ~11.8k changed lines for a purely additive change, which is hard to review. The content matches `create_app().openapi()`.

## Verified clean
- RLS/guards (insider probe as member Bea): version skip, `created_by`/`saved_by` change, edits to a tombstoned item, updating someone else's reaction, and moving a reaction to oneself are all refused with `InsufficientPrivilege`. An attendance INSERT for someone else is refused (existing test). Composite FKs `(plan_id, x)` keep places, items, participants, and leads inside one plan. There is no DELETE grant.
- IDOR (probe): a lead from another plan → 422; a place from another plan → 422 on create and 404 on add-to-plan; updating or attending an item from another plan through your own plan path → 404. Every lookup filters by `plan_id`. The IDOR sweep includes the new tables, and the RLS sweep is catalog-driven.
- Lock order: plan row → item/place → ledger head → commitment, consistent with `open_ledger` and `link_expense`. `attend`/`react` lock only the item/place, which cannot form a cycle. Reaction and attendance first-insert races are serialized by the item/place `FOR UPDATE`. Appends are serialized by the plan row lock, and READ COMMITTED re-reads `max` after the lock (correct apart from H1).
- Sync: `place` and `itinerary_item` are registered (SNAPSHOT_ORDER, VISIBLE_TYPES, LOADERS, PAGERS). Reaction and attendance changes bump the parent version. `commitment_id` changes only through item writes, which bump the item. Loaders check `plan_id` and tombstones.
- Policy: viewers cannot contribute but can respond. Hangouts → 409 `NOT_AVAILABLE_FOR_HANGOUT` after the 404/403 checks. Deletion-scheduled and completed/archived/cancelled plans deny contribute/manage/respond (rule review). The permission matrix doc matches `PLAN_RULES`.
- Cancel semantics change: no caller or test relied on the old 409. `test_finance_budgets` still passes.
- Purge: the new function is identical to 000010 plus four planning DELETEs in FK order. `CREATE OR REPLACE` keeps the grants. FK checks have usable indexes (PK leading `item_id`/`place_id`, and `(plan_id, …)` indexes). The purge test counts `schedule_places` rows.
- Activity summaries carry no free text (category enum, ISO day, item id). `SUMMARY_KEYS` was extended.
- Maps parser never fetches anything. Short links stay `pending`. Coordinates are range-checked and quantized to `numeric(9,6)`.
- No `INSERT … RETURNING`/`ON CONFLICT`. UUIDs and timestamps are set in Python. Client-ID collisions → 409 inside a savepoint.

## Recommended actions (priority order)
1. Fix C1 (flush order in `update_item`) and add the costed-item edit tests.
2. Add `COLLATE "C"` to `order_key` in 000011 (H1) before the migration ever ships; add a mixed-case ordering test.
3. Restrict `maps_url` to http(s) (H3).
4. Decide H2 with the user, then implement (recommend: a converted commitment remembers the source withdrew it).
5. Fix the M1 500s (NaN, tz-aware time, long keys). Then M2/M3 (skip unchanged cost), M4 (`is_actor_account`), M5 (rate limit).
6. Fill the M6 test gaps. Correct the migration header lock note (L1).

## Plan follow-ups
- Slice 1 design items are implemented: tables, RLS, guards, purge, sweeps, commands, sync entities, feed events, offline Maps parsing, and docs. Do not mark slice 1 complete until C1/H1/H3 are fixed; the success criterion "costs count once in budgets" is also not asserted against budget totals.

## Unresolved questions
1. H2: which option for costs whose source was deleted or cancelled after conversion when the expense is later voided or unlinked?
2. Should an item cost require `plan.budgets.manage`, or is any contributor (guests included) intended to create budget estimates? It follows the expense pattern per the decisions, but manual commitments require manage-budgets.
3. Should a place revert to `shortlist` when its last itinerary item is deleted?
4. Which Postgres (managed? glibc/ICU collation?) will production use? H1 is latent on alpine/C and live on glibc `en_US`.
5. Unrelated databases `beluno_scout` and `beluno_scout2` exist on the :54331 cluster. They were not created by this review and were left alone.

Status: DONE_WITH_CONCERNS
Summary: Gates and 569 tests pass, but editing any itinerary item with an unconverted cost 500s (guard rejects the port's early flush). `order_key` ordering breaks on glibc-collated Postgres. `maps_url` accepts `javascript:`/`intent:` links. Voiding an expense resurrects a deleted item's cost as a permanent estimate.
Concerns/Blockers: C1, H1, and H3 block merge. H2 needs a user decision.
