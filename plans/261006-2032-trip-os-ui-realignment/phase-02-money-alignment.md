---
phase: 2
title: "Money alignment"
status: in-progress
priority: P1
effort: "~2 weeks"
dependencies: [1]
---

# Phase 2: Money alignment

## Overview

Close the gaps between the finance module and the money screens: hangout limits, expense time, the adjustment split, personal spend, a changeable base currency, market rate estimates, settling everything in the base currency at frozen rates, ledger confirmation, a "settled under" tolerance, kitty targets and counts, and readable expense history. The ledger rules stay: append-only history, balanced postings, no silent netting, and no money movement.

## Context

- UI screens: Quick expense (S41/S82/S52), Expense detail (S57/S66/S05), Split (S78/S81), Expenses list (S51/S64), Balances (S65), Proof of balance (S36), Settle up (S48), Record payment (S23), Payment recorded (S71), Budget (S09/S67), Edit budget (S42/S12), Cash kitty (S76), Record kitty (S53), Trip settings (S10), Trip home settling (S50), Hangout (S20/S73).
- Spec: `trip-os-product-blueprint.md` §9.3–9.8, §9.15, §9.21, §17.1, §17.3, §17.7, §24.9.
- Current design: `docs/adr/0003-financial-ledger.md`, `plans/261005-2356-beluno-backend-plan/phase-05-finance-ledger.md` (Execution Decisions), `src/beluno/modules/finance/`.

## Requirements and decisions

### 1. Hangouts

Budgets, cost commitments, and the fund are trip-only. Their commands return `409 NOT_AVAILABLE_FOR_HANGOUT` on a hangout. Expenses, refunds, balances, settlements, and waivers work the same in both types.

### 2. Expense time

- Revisions gain an optional `occurred_at` (an instant) and `occurred_timezone` (IANA) next to the required `occurred_on` (local date).
- When `occurred_at` is given, `occurred_on` must equal its local date in that zone.
- Lists group by `occurred_on`; detail shows "SUN, MAR 21 · 20:10 JST".

### 3. Adjustment split

- New method `adjustment`: the listed participants share the amount equally after per-person signed adjustments in exact minor units: `owed_i = adj_i + lr-v1 share of (amount − Σ adj)`.
- Every `owed_i` must be ≥ 0; violations return `422 SPLIT_INVALID`.
- Revisions store the raw adjustments; the algorithm stays `lr-v1`.

### 4. Personal spend

- An expense is personal when its only payer is also its only split participant. This is derived, not a flag; it posts nothing.
- The plan setting `count_personal_spend` (default true) decides whether the budget view counts personal spend ("Count personal expenses").

### 5. Base currency change

- Replace `409 BASE_CURRENCY_LOCKED` with the command `plan.change_base_currency`, which needs `budgets.manage`.
- With finance data present, it requires one rate from the old base to the new base (`manual`, or `estimated` from the rate feed) and records an immutable `finance.base_currency_changes` row with its snapshot.
- Original amounts, revisions, postings, and settlements never change.
- Base values are read through the change chain: revision snapshot rate, then each later base change rate. Expenses already in the new base become identity.
- Budget limits and commitment base amounts are mutable rows: they are re-denominated at the same rate, with a version bump and an audit entry.
- An open consolidation (item 7) must be completed or reversed first: `409 CONSOLIDATION_OPEN`.

### 6. Market rate estimates

- Global reference table `finance.market_rates (base, quote, rate, as_of, source)`, filled by a daily worker job through a `RateProvider` port.
- `GET /v1/fx/rates?base=VND` returns the latest rates with `as_of`, `source`, and a disclaimer flag. Clients cache them for offline estimates ("1 JPY = 168.40 ₫ · market · est.").
- Expenses keep their own snapshot. Source `estimated` covers market estimates; "Use my card's rate" is `manual`.
- **Open decision: provider.** ECB reference rates are free but lack VND. Until a provider is chosen, the port has a no-op adapter and the endpoint returns an empty list.

### 7. Settle everything in the base currency ("All in VND")

- Command `ledger.consolidate` (owner or admin) freezes one rate per foreign currency (from the feed or entered manually). It then appends one `conversion` entry per foreign currency, moving every participant balance in that currency into the base currency at the frozen rate.
- Base-side amounts use largest remainder so each entry stays zero-sum in both currencies.
- Source rows: `finance.consolidations` and `finance.consolidation_rates` (immutable), referenced by the transactions. The database verifies zero-sum per currency; reconciliation re-derives the converted amounts.
- Requires zero fund availability in each converted currency (`409 FUND_NOT_EMPTY`), so the kitty pays out first.
- Reversible while no settlement has been recorded after it (`ledger.reverse_consolidation`), otherwise `409`.
- After consolidation, balances, preview, and settlements are plain base-currency ones. Later foreign expenses create new per-currency balances again.
- The UI's "Per currency" mode needs nothing new.

### 8. Ledger confirmation

- `ledger.confirm` (self, intent command) stores `(plan_id, participant_id, ledger_seq)`.
- The `ledger` sync entity lists who confirmed the current `ledger_seq` ("Ledger confirmed by 6 of 6", "Waiting on An, Huy"). Any new entry makes earlier confirmations stale.
- Confirmation is informational; it never blocks recording payments.

### 9. Settled-under tolerance

- Plan setting `settle_tolerance_minor` (base currency, default 0, at most 1 % of 10^12).
- Base-currency balances with |b| ≤ tolerance count as settled for the ledger status ("settled") and are left out of preview transfers.
- Other currencies use zero tolerance. Postings stay exact.

### 10. Kitty

- Fund settings gain an optional per-member target (`target_currency`, `target_minor`). The fund view shows contributed vs target per member ("¥5,000 of ¥10,000").
- `fund.count` (custodian or managers) records an immutable count `(currency, counted_minor, expected_minor, note)`. The view shows "Last count matches" or the difference.
- Counts post nothing. A shortage is fixed with a normal fund-paid expense, a surplus with a contribution.

### 11. Readable history

- The revisions endpoint and the expense entity expose per revision `source` (`http`/`sync`), `client_created_at`, and the device label of the session that wrote it.
- Clients render "created offline", "synced from Minh's phone", and field diffs ("Equal → Shares", "added a second payer") from consecutive revisions.

### Settings placement

`count_personal_spend` and `settle_tolerance_minor` live on the finance side (ledger head columns via `ledger.configure`) so finance keeps owning money rules. The trip settings screen reads them from the `ledger` entity.

## Execution Decisions (user, 2026-10-06)

- **FX provider:** none yet. Build the table, endpoint, daily job, and `RateProvider` port with a no-op adapter; the endpoint returns an empty list until a provider is chosen. Manual rates always work.
- **Who may consolidate:** owner and admin only (`ledger.consolidate`, `ledger.reverse_consolidation`). The `budgets.manage` capability does not grant it: consolidation changes everyone's balances, not the budget.
- **Branch:** `feat/money-alignment`, cut from the realignment branch (PR #5 merges on its own).

## Architecture notes

- Migration `000008_money_alignment`:
  - revision columns
  - split method list
  - `base_currency_changes`, `market_rates`, `consolidations`, `consolidation_rates`, `ledger_confirmations`, `fund_counts`
  - ledger head setting columns; fund target columns
  - RLS, grants, and append-only triggers for the new canonical tables
  - deferred-trigger updates: `expected_postings` for consolidation conversions, `verify_transaction` kind checks
- `market_rates` is reference data: SELECT for `api_runtime` and `worker_runtime`, INSERT for `worker_runtime` only, no RLS tenant scope.
- Base-value reads (budget view, expense base display) go through one helper that walks the change chain. Unit test it with golden cases.
- New commands join the finance kill switch and rate limit.

## Related Code Files

- Create:
  - `alembic/versions/000008_money_alignment.py`
  - `src/beluno/modules/finance/{consolidation,base_currency,market_rates,confirmations}.py`
  - `src/beluno/worker` rate ingest task and provider port
- Modify:
  - Finance: `src/beluno/modules/finance/{splits,expenses,budgets,commitments,funds,ledger,views,preview,states,fx}.py`
  - `src/beluno/db/models/finance.py`, `src/beluno/contracts/finance.py`
  - API: `src/beluno/api/{finance_presenters,finance_projection}.py`, `src/beluno/api/commands/finance.py`, `src/beluno/api/routers/finance.py`
  - `src/beluno/modules/plans/service.py` (base currency routing)
  - Docs: `docs/adr/0003-financial-ledger.md`, `docs/contracts/error-catalog.md`, `docs/runbooks/finance-operations.md`
- Tests:
  - New: `tests/unit/test_finance_money.py` (adjustment split, base chain, consolidation rounding), `tests/integration/test_finance_consolidation.py`, `tests/integration/test_finance_base_currency.py`
  - Updates to the expense, budget, fund, and settlement suites, `tests/sync/test_finance_ledger_model.py`, `tests/security/test_finance_ledger_guards.py`

## Implementation Steps

1. Confirm open decisions with the user (FX provider; who may consolidate) and record them as Execution Decisions in this file.
2. Hangout guards and expense time (small, unblocks the hangout screen).
3. Adjustment split and personal spend in the money kernel, golden and property tests first.
4. Migration `000008` and models.
5. Ledger settings (`ledger.configure`), tolerance in status and preview, and ledger confirmation.
6. Kitty targets and counts.
7. Rate feed port, table, endpoint, and job (no-op adapter until a provider is chosen).
8. Consolidation and its reversal, extending the reference model test with consolidation steps.
9. Base currency change with the change chain, budget and commitment re-denomination.
10. Readable history fields.
11. OpenAPI, docs, runbook, full gates.

## Success Criteria

- [ ] Every money screen in release 1 renders from API or sync data without client-side ledger math beyond display, rate estimates, and revision diffs.
- [ ] Consolidation keeps every transaction zero-sum per currency. Reversal restores balances exactly. The reference model converges with consolidation steps.
- [ ] A base currency change leaves every original amount and posting unchanged, and budget totals equal a recomputation from originals through the chain.
- [ ] Hangouts reject budget, commitment, and fund commands; trips are unaffected.
- [ ] Ledger confirmations go stale on the next entry. Tolerance never alters postings.
- [ ] Full gates green on real PostgreSQL.

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Consolidation rounding breaks zero-sum | Largest remainder on the base side with a property test over random balances and rates. |
| Base change chain drifts from recomputation | Single helper with golden tests; reconciliation compares the budget view against recomputation. |
| Rate provider unavailable | Feed is optional; manual and client-estimated rates always work. |
| Scope creep into payments | No provider, bank, or wallet integration; DESIGN.md forbids in-app payments. |
