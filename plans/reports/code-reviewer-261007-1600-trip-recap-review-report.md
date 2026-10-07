# Code review: trip recap (`GET /v1/plans/{id}/recap`), branch feat/trip-recap

## Scope
- Files: `src/beluno/modules/recap.py` (202), `src/beluno/contracts/recap.py` (69), `src/beluno/api/routers/recap.py` (73), `src/beluno/api/main.py` (+2), `tests/integration/test_recap.py` (208), README, permission-matrix, phase-06 notes, `openapi/openapi.json` (+426, additive)
- Gates: ruff check/format clean, mypy strict clean, `tests/integration/test_recap.py` 3 passed on Testcontainers PG (not skipped), committed OpenAPI equals app export.
- Probes: scratch pytest (`-p conftest`, Testcontainers) for departed debtor + tolerance, non-base tolerance, top place after leaves, datetime-mode trip. All four reproduced defects below.

## Overall
Money totals correctly reuse `get_budgets` (refunds, voided, personal-spend setting, merged participants, base-currency chain, unconverted all inherited), and RLS on places/polls/itinerary is plan-wide `actor_is_active_participant`, so counts do not vary by role. The "settled" block re-derives ledger state with different rules from `Ledger.everyone_settled`/`suggest_settlements`, and the result disagrees with the ledger screen in reproducible cases. That is the main blocker: the S58 "Zero balance remaining · SETTLED" claim can be false.

## Critical
None.

## High

**H1. `all_settled` true while a departed participant still owes money** (`modules/recap.py:71-85`, `:173-194`, `:62-64`)
- Crew = ACTIVE participants only; `all_settled = all(crew)`. Leave/remove do not require a zero balance (`modules/plans/participants.py:326-364`).
- Repro (probe): tolerance 100; Ann pays 180 split Ann+Dan, Bea pays 180 split Bea+Dan → Ann +90, Bea +90, Dan -180; Dan leaves. Recap: `all_settled: true`, every crew row settled. Ledger: suggestions Dan→Ann 90, Dan→Bea 90. Same without tolerance if both sides of a debt leave or are removed.
- Fix: compute settled-ness over every participant ledger account (as `Ledger.everyone_settled` does), not only over the crew list. Either derive `all_settled` from "no suggested transfers" via `views.suggest_settlements(...)` (reuses the ledger's own rules), or from `LedgerHead.status == "settled"`. Keep `crew` for display, but optionally add departed people with open balances as `settled: false` entries, or add an `outstanding` flag.

**H2. Tolerance applied to every currency; the ledger applies it to the base only** (`modules/recap.py:176-188`)
- `func.abs(balance) > tolerance` with no currency filter. `ledger.py:274-281` and `views.py:103-121` use `tolerance if currency == base else 0`. The tolerance is in base minor units, so for EUR/JPY accounts it is also the wrong unit.
- Repro (probe): base USD, tolerance 100; EUR 100 split Ann+Bea → Bea -50 EUR. Recap: all crew settled, `all_settled: true`. Ledger: open, EUR transfer Bea→Ann 50 suggested.
- Fix: same as H1. Reuse `suggest_settlements(accounts, base, tolerance)` (or a shared helper extracted from `everyone_settled`) instead of a second implementation. Then recap "settled" equals ledger "settled" by construction (see memory lens "ledger status and settlement preview must agree").

## Medium

**M1. "SETTLED" with no money or with disputed settlements; `settled_on` semantics** (`modules/recap.py:112`, `:197-202`)
- Empty plan → `all_settled: true` (the test asserts it at `test_recap.py:179`), while ledger status is `open`. S58 would print "Zero balance remaining · SETTLED" for a trip with no expenses.
- `Settlement.status != "reversed"` includes `disputed`. A disputed settlement stays posted, so recap says SETTLED, and its `occurred_on` can become `settled_on`, while the ledger screen shows `disputed_settlements > 0`.
- `settled_on` = max user-entered `occurred_on` of any live settlement, including ones that happened before a later expense, backdated ones, or future-dated ones. If balances zeroed through a void after the last settlement, the date is stale. Squaring without any settlement gives `all_settled: true, settled_on: null`.
- Fix: align with `next_ledger_status` (SETTLED requires a live settlement), expose `disputed_settlements` or treat disputed as not settled, and document `settled_on` as "latest live settlement date". Product call on the empty-plan case; at minimum don't assert `True` for it in tests.

**M2. Top place counts reactions from departed and merged participants** (`modules/recap.py:152-170`)
- No join to `PlanParticipant.access_state == 'active'`. `planning/places.py:189-206` (`place_views`) deliberately excludes them ("People who left or were removed no longer count").
- Repro (probe): 3 users want place P; Bea and Dan leave. Recap `wanted_by: 3, of_people: 1` ("3 of 1"); places view `wanted_by` = 1 person. A merged guest's reaction stays under the merged participant row, so guest + survivor double-count.
- Fix: join `PlanParticipant` on `PlaceReaction.participant_id` and filter `access_state == ACTIVE`, matching `place_views`.

**M3. Share card total can be wrong or 0 with no signal** (`api/routers/recap.py:65-72`, `modules/recap.py:94`)
- `share.spent_minor` excludes unconverted spending, but `share` carries no `unconverted` flag. Probe: EUR-only spending with no rate → `spent_minor: 0, unconverted: true`. A card drawn from `share` alone would say "spent 0". `estimated_rates` from the overview is also dropped. Same understatement hits `per_person_per_day_minor`.
- Fix: put `unconverted` (and `estimated_rates`) on the share card and the response, or make `share.spent_minor` nullable when anything is unconverted.

**M4. Datetime-mode plans have no start date on the card or response** (`api/routers/recap.py:41-42, 67`)
- `start_date`/`end_date` come from `plan.start_date`/`end_date`, which are always NULL in `datetime` mode (contract forbids them). Probe: datetime trip → `start_date: null, end_date: null, days: 8`, `share.start_date: null`, so S32 cannot show the month. `_plan_days` already computes local dates and throws them away.
- Fix: have `_plan_days` return `(start, end, days)` (local dates in the plan timezone) and use those for both response and share.

## Low

- **L1. `days` edge cases** (`modules/recap.py:116-126`). An `ends_at` at exactly local midnight counts an extra day (probe: 23:30 Mar 8 → 00:00 Mar 15 NY, across the DST change, gives 8). A 19:00–01:00 hangout gives `days: 2`, which halves `per_person_per_day_minor` for one evening. DST itself is fine (date arithmetic after zone conversion). Consider `(ends_at - 1µs)` or document it. Product call for hangouts.
- **L2. `per_person_per_day_minor` denominator** (`:93`). It divides total spend, which includes consumption by people who left, by the current active headcount (placeholders included, pending excluded). This may be intended ("6 friends"); document it in the field description.
- **L3. Basis points may not sum to 10 000** (`:95-102`). Each category is rounded half-up on its own, so the sum can be off by ±(n-1). A negative-net category (if refunds can exceed a revision's amount) is filtered by `> 0` but stays in `spent`. Use largest-remainder rounding if the UI sums them; otherwise document it.
- **L4. `type: ignore` hacks.** `modules/recap.py:147` can be avoided by typing `*where: ColumnElement[bool]`. `api/routers/recap.py:51`: use `cast(ExpenseCategory, category)` or type `Recap.categories` with `ExpenseCategory`. An unknown DB category would turn into a 500 ResponseValidation either way.
- **L5. Magic strings** `"done"`, `"cancelled"`, `"closed"`, `"reversed"` (`:106-110, :200`) duplicate `planning.itinerary.DONE/CANCELLED`, `planning.polls.CLOSED`, and `SettlementStatus.REVERSED`. Import the constants. A closed poll with zero votes counts as a "decision".
- **L6. `_stop`** (`:135-138`). `stop["name"]` raises KeyError → 500 on a malformed row. The DB CHECK only enforces array/length; writes go through the validated `Destination` model, so the risk is low (invites already index `item["name"]`). `.get("name")` with a skip would be more defensive at no cost.
- **L7. OpenAPI responses** `problem_responses(401, 404, 503)` omit 422 (bad UUID path), unlike finance `READ_ERRORS`. 403 is unreachable today (VIEW_FINANCE allows all roles).
- **L8. Redundant reads.** `get_budgets` already loads every participant, and recap runs a second participant query plus about 7 sequential scalar queries. Bounded and fine for a non-hot screen. No N+1, and all filters are on plan-scoped indexed columns. The statements run in READ COMMITTED, so the money and balance blocks can come from different snapshots under concurrent writes (cosmetic, read-only).
- **L9. Contract doc** `RecapPerson.settled` says "Every balance within the plan's settle tolerance". That is wrong for non-base currencies (see H2); fix the text along with the code.

## Checked, no issue
- Authz: `get_budgets` → `load_plan` + `require_plan(VIEW_FINANCE)`. Pending/left get HIDDEN → 404, strangers 404, deletion-scheduled allowed (consistent with ledger/budgets). Hangouts work (test covers it).
- Privacy: `share` has no title, names, balances, codes, addresses, or notes. The main response exposes only participant IDs, place name, and destination names, all already visible to VIEW_FINANCE holders. Booking secrets are never read (test asserts this).
- Money equivalence with the budget screen: identical `overview.total.actual`, and categories are summed from the same `base_net`. Refund reversals, voided expenses, personal spend, merged split resolution, and the base-change chain are inherited.
- Merged participants: balances move to the survivor (`finance/merges.py`), so merged accounts are zero and excluded correctly from crew.
- Fund account excluded from per-person settled (matches the ledger).
- RLS: places/reactions/polls/itinerary SELECT policies are `actor_is_active_participant(plan_id)`, so there is no role-dependent undercount.

## Test gaps (`tests/integration/test_recap.py`)
Missing: tolerance (base vs non-base), departed participant with balance, disputed/reversed settlement, refund/void, `count_personal_spend=false`, base-currency change, unconverted spending in totals and share, basis-point rounding with 3 categories, datetime-mode trip start date, viewer/guest/pending access, top place after leave/merge. The empty-plan `all_settled is True` assertion locks in M1.

## Recommended actions
1. H1+H2: replace `_settled` with the ledger's own rule (`suggest_settlements` or a shared helper from `everyone_settled`) over all participant accounts; add tests for departed debtor and non-base tolerance.
2. M2: active-participant join in `_top_place`.
3. M1: settled requires a live settlement, account for disputes, and pin down `settled_on` semantics.
4. M3/M4: unconverted flag on share; local start/end dates for datetime plans.
5. L4/L5 cleanup.

## Metrics
- Type coverage: mypy strict clean (2 `type: ignore` added)
- Tests: 3 integration tests pass; coverage not measured for this slice
- Lint: 0

## Unresolved questions
- Should a plan with no money ever show "SETTLED"? (Ledger says `open`.)
- Should people who left with an open balance appear in the crew manifest as not settled?
- For hangouts, should `days` be 1 for a same-evening event that crosses midnight?
