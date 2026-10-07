# Code review: trip and account export (feat/trip-export, uncommitted)

## Scope
- Files: src/beluno/api/exports.py (new, 219), src/beluno/api/routers/exports.py (new, 66), src/beluno/api/main.py, src/beluno/modules/iam/rate_limits.py, tests/integration/test_exports.py (new, 188), README.md, docs/contracts/permission-matrix.md, docs/contracts/retention-matrix.md, openapi/openapi.json, phase-06 + plan.md
- Focus: uncommitted diff + untracked files
- Verification run (Docker Testcontainers):
  - ruff check / format --check / mypy strict: clean
  - full suite: 600 passed; coverage total 95%, exports.py 95% (missed 158, 188, 193)
  - OpenAPI regenerated to scratch == committed file; `check_openapi_compatibility.py main..HEAD` exit 0
  - Reviewer probes (scratch pytest, not committed):
    - `exercise_money_and_members` then JSON export vs `/v1/sync/pull` snapshot for owner and viewer Dan: exported id sets == synced upsert id sets for every entity type (no extra, no missing)
    - pending participant: hidden from member export (id and name), visible to owner, pending user's own plan export 404, account export lists no plan for them
    - removed participant: plan export 404; account export `plans` empty
    - hangout: CSV and JSON both 200 (planning types present as empty arrays)
    - PAGE_SIZE patched to 2, 1..5 expenses: CSV rows 1,2,3,4,5 (boundary correct)
    - formula probes: names `=cmd|...` and `@Evil` escaped in paid_by/split; leading `\n`/space stripped by input validation before storage; full-width `＝1+1` NOT escaped

## Overall assessment
Solid, small, and correctly reuses the sync projectors, so (a) secret/visibility handling is provably identical to sync. Authorization (b) is correct. No Critical or High issue. Main risk is resource use: whole export built in memory, serialized on the event loop, inside one open transaction with no bound.

## Critical
None.

## High
None.

## Medium

M1. Unbounded synchronous build inside the transaction; CPU-bound JSON on the event loop
- exports.py:74, 118 (`_document` called inside service, so inside `open_context`); exports.py:106-119 (account loop); exports.py:161-167 (`json.dumps(indent=2)`)
- Scenario: account with many plans (hangouts are free/unlimited) or a plan with thousands of expenses. Account export = 2 queries per plan for `scope_access` + ~20 pagers per plan (expense/booking pagers add view sub-queries), all sequential on one pooled connection in one transaction; then dict list + JSON str + UTF-8 bytes (~3 copies) and `json.dumps(indent=2)` blocks the worker loop for every other request. No statement/idle timeout exists in the repo. 20/h/user does not bound size per request; many guest/member accounts can each do it.
- Fix (keeps "synchronous download, nothing stored"):
  1. Return entities from the service; render after `open_context` exits so the connection is released before serialization.
  2. Serialize off-loop: `await anyio.to_thread.run_sync(_document_bytes, ...)` (same for CSV writer).
  3. Drop `indent=2` (≈25-35% smaller) or make it compact by default.
  4. Consider a hard cap (plans per account export or rows) returning a problem, or stream per plan via `StreamingResponse` after the snapshot is taken.

## Low

L1. Snapshot not point-in-time (READ COMMITTED)
- exports.py:137-158; db/session.py:92-94
- Each pager sees a new snapshot; a concurrent expense/settlement between the `expense` and `ledger` pagers yields a JSON export whose ledger disagrees with its expenses. Sync tolerates this via cursors; a one-shot export does not.
- Fix: optional `isolation_level` on `Database.transaction` (must run before `set_actor_context`, since `SET TRANSACTION` has to be first), REPEATABLE READ for exports. Accept as known limitation if not worth it.

L2. Formula-escape gaps
- exports.py:34, 177, 194-203
- `FORMULA_STARTS` lacks full-width `＝ ＋ － ＠` (U+FF1D/FF0B/FF0D/FF20); probe confirmed `＝1+1` exported raw. Some CJK Excel setups normalise these. Low because description/name input is otherwise stripped of leading whitespace/newlines (verified).
- Escaping is applied per name inside composite cells, so a second payer gives `Ann: 5.00; '@Evil: 5.00` (stray apostrophe mid-cell, cosmetic). Only cell start matters; escape the joined cell once instead.
- category, currency, rate, rate_source, state, ids, dates are enums/numeric: safe (verified contracts).

L3. Authorization bypasses the policy module
- exports.py:122-126 uses `scope_access` directly; CLAUDE.md pattern is `load_plan` + `require_*` with rules in `authorization/policy.py`.
- Behaviour matches the user decision and sync (active participants only, allowed during deletion like `PlanAction.VIEW`). Risk is drift: a future policy change (e.g. guest finance restriction) would not reach exports. Add `PlanAction.EXPORT` with `Rule(ALL_PLAN_ROLES, allowed_during_deletion=True)` or document that exports follow sync access by design.

L4. Private packing fetched across all plans
- exports.py:128-133: loads every private packing item the user owns (all plans) via the user-scope pager, then filters in Python. Fine at current scale; add a plan filter (index `packing_items_owner_idx` is (owner_user_id, id), so a `plan_id` predicate is cheap enough).

L5. CSV semantics worth documenting (not decision reversals)
- Voided expenses are rows with full amounts (`state=voided`); summing `amount` overcounts. Refunds are shown but not netted.
- After a base-currency change, older rows keep the old `base_currency` (probe: plan moved to EUR, rows show USD; no-rate JPY row has empty base). Labelled per row, so correct, but mixed-base columns don't sum. `base_change_number` is not exported.
- `exponents.get(code, 2)` (exports.py:189) silently defaults; fine if currencies FK guarantees presence, else fail loudly.
- `FUND = "Kitty"` hard-coded English label; names containing `;`/`:` make paid_by/split ambiguous.

L6. OpenAPI declaration
- routers/exports.py:18-24: `/v1/me/export` advertises `text/csv` and 404, neither reachable. No response schema for JSON body and no `headers` (Content-Disposition, Cache-Control) declared. Split FILE_RESPONSES per route; optionally declare headers.

L7. CSV sort key compares ISO strings
- exports.py:97: pydantic omits zero microseconds, so `...:00Z` sorts after `...:00.5Z` in the same second. Sort by `(occurred_on, id)` (UUIDv7 is time-ordered) instead.

## Test gaps (g)
- Uncovered lines in exports.py: 158 (second page), 188 (null base amount), 193 (kitty payer). Probes show they work; repo tests don't prove it.
- Missing cases: formula in participant/placeholder names; pending participant hidden from a member's JSON/CSV; plan export 404 for left/removed participant (only account export covers "left"); guest export (explicitly in user decision); hangout export; voided + refunded rows; 429 after 20 hits (and whether 404 consumes quota); booking `private_notes` absence (only confirmation code checked).
- Weak assertion: `not any("token" in invite ...)` checks only a key named `token`; assert the raw tokens returned at creation are absent from `response.text`.
- A sync-equivalence test (export ids == `pull_all` upsert ids, as in the probe) would lock in the core guarantee in one test.
- No vacuous asserts found; no mocks of own code.

## Verified OK (a)-(h)
- (a) No booking secrets, invite tokens, other users' private packing, other users' sessions (user-scope pager filters `user_id == scope_id`), or pending participants for non-managers. Equivalence with sync proven by probe.
- (b) Non-participant, pending, removed -> 404 (RLS + `scope_access`); no 403/404 oracle. Hangouts work.
- (c) Decimals by exponent correct (JPY `3000`, USD `12.34`); BOM via `utf-8-sig`; base snapshot columns present.
- (d) Rate limit before transaction, same pattern as booking reveal; counted on 404, not on 422 (FastAPI validation precedes handler). Pagination loop correct at boundary.
- (e) `record_audit` inside `open_context`, flushed with the transaction; metadata only `{"format": ...}`; no audit on 404.
- (f) Filenames built only from validated UUIDs: no header injection. `Cache-Control: no-store` set.
- (h) Router registration consistent; quality gates and full suite green; OpenAPI committed and compatible.

## Recommended actions
1. M1: render outside the transaction and off the event loop; compact JSON; consider a size cap.
2. Add missing tests listed above (at least second-page, pending-hidden, name formula, guest, 429).
3. L2: escape full-width formula starts; escape composite cells once.
4. L3: add `PlanAction.EXPORT` or document the deliberate sync-access coupling.
5. L6/L7/L4 when convenient.

## Plan follow-ups
- Phase 06 slice 1a (exports) functionally complete per Execution Decisions; success criterion "no export carries a secret field" holds by construction and probe. Leave checkbox to lead.

## Metrics
- Type coverage: mypy strict clean
- Test coverage: 95% total; exports.py 95%, routers/exports.py 100%
- Lint issues: 0

## Unresolved questions
- Is a per-request size cap acceptable under "free, synchronous download", or should large accounts move to the background-job path planned for the receipt archive?
- Should voided expenses appear in the CSV at all, or with blank amounts?

Status: DONE_WITH_CONCERNS
Summary: Exports are secret-safe and correctly authorized (proven equal to sync snapshot by probe); gates and full suite green. Main concern is unbounded in-memory build and JSON serialization inside the open transaction on the event loop, plus test gaps.
Concerns/Blockers: M1 resource use; missing tests for pagination, pending visibility, guests, 429.
