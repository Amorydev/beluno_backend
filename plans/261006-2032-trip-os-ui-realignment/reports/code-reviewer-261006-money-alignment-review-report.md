# Code review: money alignment (`feat/money-alignment`, 3f6f5aa..1a4bc34)

## Scope

- Commits: 3f6f5aa, 76715c3, 46f6090, d3bf427, 115e2d2, 52565b4, 836f2a6, 1a4bc34 (diff vs `fix/finance-refund-split-lock-and-utc-sessions`; the docs commit ea0c577 that landed during the review is out of scope)
- Files: 50 files, +7.7k / -1.8k (openapi.json accounts for about 5.3k). Core: `alembic/versions/000008_money_alignment.py`, `src/beluno/modules/finance/{consolidation,base_currency,ledger_settings,market_rates,ledger,budgets,preview,funds,expenses,commitments,splits,postings,views}.py`, `src/beluno/api/**`, `src/beluno/contracts/finance.py`
- Checked against: `docs/adr/0003`, `000005_financial_ledger.py`, the phase-02 file (Requirements 1–11, Execution Decisions)
- How I verified: read the code, diffed the replaced SQL functions against 000005, ran the 79 targeted tests on live PG :54331 (all pass), ran ruff, the format check, and strict mypy (all clean), and ran 3 throwaway live-DB probes from the scratchpad (results below). I did not modify the repo.

## Overall

The ledger plumbing is sound. The replaced `expected_postings`/`verify_transaction` behave exactly like 000005 for every old kind. Lock order is consistent. Every new table has RLS, grants, and append-only triggers. Authorization matches the Execution Decisions, and the hangout guards are complete.

There is one high-severity defect, which the new changeable base currency introduces: rates are interpreted against whatever the base currency is when the request arrives. There are three medium issues: a tolerance and preview inconsistency, consolidation invariants that the database checks only when the consolidation is inserted, and a missing reconciliation requirement.

## Critical

None.

## High

### H1. A rate's quote currency is implicitly "the base right now", so stale or offline requests are silently reinterpreted after a base change
- `src/beluno/modules/finance/expenses.py:589-610` (`_base_amount`: `quote=base_currency` taken from the current plan row)
- `commitments.py:262-279` (`_apply`, same)
- `consolidation.py:110,151-159` (`base = plan.base_currency`; each rate is recorded as currency→current base)
- Contracts: `RateRequest` (`contracts/finance.py:65`), `ConsolidationRateRequest` (`:601`). Neither carries the currency the rate is quoted in.
- Before this branch this was safe: `plan.update` refused a base change once finance data existed (the removed `ledger_exists`/`BASE_CURRENCY_LOCKED`). Now the base can change while devices are offline (ADR 0004), and nothing ties a rate to the base the client saw. Expense and commitment versions are not bumped by a base change, so If-Match does not catch this either.
- Verified on live PG (base USD, all spending in JPY, owner changes the base to VND at 25000):
  - A consolidation request prepared under USD (`JPY rate 0.0067`) returns 201. It posts 2,000 JPY → **13 VND** (lines +13/−7/−6). That is real postings. They can be undone only until someone records a payment.
  - An expense of 1,000 JPY with `base_rate 0.0067` returns 201 and is stored as base `{currency: VND, amount_minor: 7}`. It counts as 7 VND in `/budgets`; the correct value is about 168,000.
- When it happens: the expense and commitment cases need only one manager to change the base while another device has a queued expense that carries a rate. That is the normal offline flow. The consolidation case also needs the old base to have no open balances; otherwise the "one rate per open currency" check rejects the request by accident.
- Fix: make the quote explicit.
  - Add `quote_currency` to `RateRequest` and require `base_currency` on `ConsolidateRequest`. Reject with 409 (for example `BASE_CURRENCY_CHANGED`) when it differs from `plan.base_currency`.
  - Alternative: accept a quote equal to an earlier base and convert it through the chain, but explicit rejection is simpler and keeps snapshots honest.
  - Add a sync push test that queues an expense, changes the base, and then pushes.

## Medium

### M1. The settle tolerance drops small debtors one by one, so the preview can be empty while the ledger is not settled
- `src/beluno/modules/finance/preview.py:48-57` filters each balance with |b| ≤ tolerance before matching. `ledger.py:273-282` (`everyone_settled`) requires every |b| ≤ tolerance.
- Verified with `simplify_debts({A:+200, B..F:-40 each}, 0, tolerance=50)`:
  - The preview returns `transfers=[]`, but A (+200 > 50) keeps the status `open`/`reopened` forever.
  - The Settle up screen shows nothing to do while Balances shows A is owed 200.
  - The mirror case (one debtor, many small creditors) behaves the same way.
- Postings stay exact (good). The problem is that the status and the preview disagree.
- Fix (product call): exclude parties only while the excluded total stays within the tolerance of every remaining counterpart, or compute transfers on exact balances and drop only transfers whose both parties end within the tolerance. Keep the invariant "status settled ⇔ preview empty", and add a unit test for it.

### M2. Consolidation invariants are checked only when the consolidation row is inserted
- `000008_money_alignment.py:360-365,566-588`. `consolidations_complete` fires only on `INSERT ON finance.consolidations`. `consolidation_rates` and `consolidation_lines` have no completion trigger. `guard_consolidation` allows `state → reversed` without any ledger entry.
- In 000005, every child insert re-verifies its parent (`expense_payers_complete`, `refund_shares_complete`, `ledger_postings_balanced`). ADR 0003 and the security suite claim that invariants hold when the application is bypassed.
- Verified as `api_runtime` with the owner as actor, after a committed consolidation:
  1. Inserting an EUR `consolidation_rates` row plus two lines (±500 EUR) for the existing consolidation commits. `GET /ledger/consolidations` now lists EUR lines that were never posted.
  2. `UPDATE consolidations SET state='reversed', version=version+1, ...` commits with no `conversion_reversal`. The API then refuses the real reversal (409 "already reversed") while the balances stay consolidated. `open_consolidation_exists` also turns false, which bypasses `CONSOLIDATION_OPEN`.
- No current app path does this, so this is defence in depth. It is still the exact gap the task asked about.
- Fix (000008 is unreleased, so it can be amended in place; otherwise use 000009):
  - Add deferred constraint triggers on `consolidation_rates`/`consolidation_lines` INSERT that run `verify_consolidation`, plus `verify_transaction` of its conversion.
  - Extend `verify_consolidation` to check that each currency's lines sum to 0 in both columns, that `line.currency <> consolidations.base_currency`, and that each rate's snapshot is `(currency → base_currency)`.
  - Add a deferred trigger on `consolidations` UPDATE requiring `state='reversed'` ⇔ exactly one `conversion_reversal` with that `consolidation_id`.
- Related, lower impact: `ledger_confirmations_insert` and `fund_counts_insert` do not tie `participant_id`/`counted_by_user_id` to the actor. This matches existing tables, and the app enforces it.

### M3. Plan requirement not built: reconciliation does not re-derive consolidations or the base chain
- The phase file (item 7: "reconciliation re-derives the converted amounts"; Risk table: "reconciliation compares the budget view against recomputation") is not implemented.
- `finance.reconcile_plan` and `src/beluno/modules/finance/maintenance.py` are unchanged. Nothing re-checks `consolidation_lines.base_amount_minor` against the frozen rate, and nothing compares budget totals with a recomputation through the chain.
- The reference model test (`tests/sync/test_finance_ledger_model.py`, `Model.consolidation`) computes its expected amounts with production `convert` and `consolidation_amounts`. It therefore cannot detect an allocation bug. Only the unit property test in `tests/unit/test_finance_money.py:472` checks that independently.
- Fix:
  - Add a reconciliation finding (`consolidation_mismatch`) that recomputes each currency's total from the snapshot rate and checks that each line is within 1 unit of `balance × rate` and that each side sums to 0.
  - Or record this as a deliberate scope cut in the phase file.

## Low

- **L1. The base chain is not enforced in the database.**
  - Verified as `api_runtime`: `UPDATE plans.plans SET base_currency='JPY'` (manager write guard) and `UPDATE finance.plan_ledger_heads SET base_change_count=0` both commit. `/budgets` then labels EUR-cent totals as `JPY`.
  - No app path does this (`plan.update` no longer accepts `base_currency`).
  - Fix if you want defence in depth: a deferred check that a base change of `plans.base_currency` on a plan with a ledger head has a matching `base_currency_changes` row (`to_currency = NEW.base_currency`, `change_number = head.base_change_count`), and `guard_ledger_head` allowing `base_change_count` only to grow by 1.
- **L2. Read skew in the budget view.**
  - `budgets.py:184-185` reads `plan.base_currency`, then `base_chain` reads the change rows in a later statement (READ COMMITTED).
  - If a base change commits in between, a single response mixes values valued as the old base (returned as-is) with values converted into the new base, all labelled with the old base. It is transient.
  - Fix: derive `current` from `steps[-1].to_currency` when there are steps, or read both in one statement.
- **L3. Contract changes not recorded.**
  - `PlanUpdateRequest.base_currency` was removed (`extra="forbid"`, so old clients and queued offline `plan.update` operations get 422).
  - Response enums and unions were widened: `conversion_reversal` in `TransactionResponse.kind`/`ExplanationEntry.kind`, and `AdjustmentSplit` in `RevisionResponse.split`.
  - `scripts/check_openapi_compatibility.py` cannot detect either kind of change. It passes against both the fix branch and `main` (verified), and `accepted-breaks.json` has no entry for them.
  - The migration header says these are accepted before launch. Record them in `accepted-breaks.json` for traceability, or extend the checker to catch removed request properties and response enum widening.
- **L4. The `_rebase` clamp is dead code.**
  - At `base_currency.py:205-207`, `convert` raises `AMOUNT_OUT_OF_RANGE` (`fx.py:51-52`) before `min(..., MAX_AMOUNT_MINOR)` runs.
  - A budget limit that converts beyond 10^12 therefore fails the whole base change with 422 instead of being clamped. It only matters for extreme limits.
  - Fix: catch and clamp, or document that the change is refused.

## Checks requested by the task

- **(a) Money correctness:**
  - Consolidation splits the rounded converted total by largest remainder on both sides, so it is zero-sum per currency (property test plus the database's per-currency sum check).
  - Reversal goes through `Ledger.reverse` and `resolve_party`. It matches the SQL path (`resolve_participant`, `HAVING sum<>0`), and zero amounts are filtered in `append`. Exact after merges.
  - The tolerance touches only the status, the merge SQL, and the preview, never postings.
  - The adjustment split is correct for a negative rest and enforces owed ≥ 0, and the owed amounts sum to the amount.
  - Defects: H1 and M1.
- **(b) Database invariants:**
  - The textual diff against 000005 shows only additions: the `conversion_reversal` reversal branch and the consolidation-conversion branch, routed before the settlement branch. The settlement-conversion check is now gated on `settlement_id IS NOT NULL`, and the new `ledger_transactions_conversion_source` check makes the two ids mutually exclusive, so old kinds behave identically.
  - The auto-named constraints `ledger_transactions_check1`/`check5` were confirmed to be the reversal and settlement checks on a 000006 database.
  - All 7 new tables have RLS and only SELECT/INSERT grants (UPDATE only on `consolidations`, behind a guard). `market_rates` has INSERT for `worker_runtime` only. New functions have EXECUTE revoked from PUBLIC, and the SECURITY DEFINER ones pin `search_path`.
  - Gaps: M2 and L1.
- **(c) Concurrency:**
  - Every new writer takes the plan row lock (`load_plan(for_update)`), then the head (`lock_head`), then its own rows: budgets `FOR UPDATE` in the base change, the consolidation row in the reversal.
  - Confirm and configure serialize on the head, and LEDGER_CHANGED is evaluated under that lock.
  - Only L2 (a read) remains.
- **(d) Sync:**
  - Consolidation create and reverse each record one change for the `consolidation` entity at the consolidation's version, and the ledger is touched.
  - `fund_count` uses version 1 because counts never change. The base change records the plan change, one `budget_rebased` change per budget, and a ledger touch.
  - Configure and confirm touch the ledger. The projection loaders, pagers, and fixtures are updated.
- **(e) Authorization:**
  - consolidate and reverse_consolidation: owner or admin. Base change: owner or admin. configure: owner or admin. confirm: every role. `fund.count`: custodian or manager. All match the Execution Decisions.
  - Hangout guards cover budgets, commitments (manual and the port), fund settings, contributions, withdrawals, counts, and every fund posting through `Ledger.append`.
  - The port has no callers, and the plan type cannot change, so no fund path is missed.
- **(f) Contract:** see L3.

## Recommended actions

1. H1: add an explicit quote/base currency to rate requests and the consolidation request, reject when it no longer matches the plan, and add a sync test that pushes after a base change.
2. M1: decide the tolerance semantics so that "settled ⇔ preview empty", then fix `simplify_debts` and test it.
3. M2: add child-insert and state-transition deferred checks for consolidations (amend 000008 while it is unreleased).
4. M3: add consolidation re-derivation to reconciliation, or record it as a deliberate scope cut.
5. L1 to L4 as convenient.

## Metrics

- Targeted tests: 79/79 pass on live PG.
- ruff, the format check, and strict mypy: clean.
- Coverage: not re-run; the tester report covers the full gate.

## Unresolved questions

1. Changing the base after the ledger is settled under the tolerance reopens it, because old-base residuals are now judged with zero tolerance. Is that intended, or should the old base keep its tolerance (re-denominated)?
2. With an active consolidation and any payment recorded after it, the base cannot change until everything is fully settled. Reversal is refused (`CONSOLIDATION_SETTLED`) and `CONSOLIDATION_OPEN` blocks the change. Is "completed" meant to be the ledger status `settled`?
3. `ledger.confirm` and `ledger.configure` create a ledger head, after which a base change requires a rate even when the plan has no money. Is that acceptable?
