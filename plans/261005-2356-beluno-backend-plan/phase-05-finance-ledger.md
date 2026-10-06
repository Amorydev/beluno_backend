---
phase: 5
title: "Financial Ledger"
status: completed
priority: P1
effort: "5 weeks (2 backend engineers + fractional QA/security review)"
dependencies: [4]
---

# Phase 5: Financial Ledger

## Context Links

- [Plan overview](./plan.md)
- [Phase 4: Reliability and Sync Kernel](./phase-04-reliability-sync-kernel.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- [Product blueprint](../beluno-product-blueprint.md) — use its money rules with `Plan` and stable `PlanParticipant` identities.
- PostgreSQL constraint triggers: <https://www.postgresql.org/docs/current/sql-createtrigger.html>
- PostgreSQL transaction isolation: <https://www.postgresql.org/docs/current/transaction-iso.html>

## Overview

Implement the authoritative financial subsystem for every plan: immutable expense revisions, balanced postings, multi-currency snapshots, settlements, budgets, booking commitments, and a virtual group fund. Canonical history is append-only; balances and summaries are disposable projections that must rebuild exactly.

## Requirements

### Functional

- Create, revise, void, refund, and explain expenses with multiple payers and equal, exact, percentage, weight, exclusion, and itemized splits.
- Record original currency amounts, explicit FX snapshots, per-currency balances, confirmed settlements, reversals, budget limits, cost commitments, and fund activity.
- Provide authoritative balance breakdowns, settlement previews, budget/actual views, fund availability, and financial audit history.
- Preserve removed participants in history and support partial settlements, fees, waivers/adjustments, and disputed or reopened ledgers.
- Emit audit, change-log, and outbox records atomically for every accepted financial command.

### Non-functional

- Use `BIGINT` minor units, pinned currency exponents, rational/integer split inputs, and deterministic largest-remainder allocation; never use IEEE floating point.
- Require `sum(payers)=expense amount`, `sum(owed)=expense amount`, and `sum(postings)=0` per transaction and currency.
- Make every mutation idempotent, version checked, plan scoped, short lived, and serial under one plan-ledger sequence.
- Never hard-delete or overwrite committed financial history; corrections append revisions, reversals, or explicit adjustments.
- Keep external FX/provider calls outside ledger transactions and retain financial idempotency for at least 180 days.

## Architecture

### Canonical write model

```text
expense ──< immutable expense_revision
                    ├──< expense_payer
                    ├──< expense_split
                    └── ledger_transaction ──< ledger_posting >── ledger_account

ledger_transaction types:
expense | expense_reversal | refund | settlement | settlement_reversal |
fund_contribution | fund_withdrawal | fund_expense | adjustment | conversion
```

- `expenses` holds stable identity and a current-revision pointer; each `expense_revision` captures amount, currency, algorithm version, resolved allocations, links, and audit metadata.
- Creating an expense appends its full postings. Editing atomically appends a transaction negating the previous effective revision, then a new full revision/transaction; voiding appends only the exact reversal.
- A participant expense posting is `paid_minor - owed_minor`; positive means the participant should receive money. Settlement debtor → creditor posts debtor `+amount` and creditor `-amount`.
- `ledger_accounts` are plan-participant or virtual-fund accounts, unique per plan and currency. Every posting references `(plan_id, account_id)` and one immutable committed transaction.
- Refunds and adjustments reference their source; cumulative refunds cannot exceed the refundable amount without an audited privileged adjustment.
- Standard expenses have one settlement currency. Cross-currency payment legs require an explicit conversion transaction and frozen FX snapshot; currencies are never silently netted.

### Database invariants and write path

Every financial command uses the Phase 4 operation/idempotency record and one short PostgreSQL transaction:

1. Lock or create `(actor, endpoint scope, idempotency key)` and compare the canonical request hash.
2. Re-authorize current plan access, validate stable participant references, then lock `finance.plan_ledger_heads FOR UPDATE`.
3. Check expected expense/settlement/budget version and increment the deterministic ledger sequence.
4. Validate currency exponent, bounds, split inputs, FX direction, links, and current commitment/fund state.
5. Append revision, allocations, transactions, postings, audit, change log, outbox, and canonical response.
6. Update critical projections, run deferred invariants at commit, then return the authoritative version and server sequence.

Same key and hash replays the stored response; the same key with a different hash returns `409 IDEMPOTENCY_KEY_REUSED`. A lost acknowledgement therefore cannot duplicate value. The ledger-head lock is the default concurrency mechanism; any later `SERIALIZABLE` path must retry SQLSTATE `40001` with a strict bound and never retry validation or authorization failures.

Direct DML is revoked. A reviewed stored finalization function plus `DEFERRABLE INITIALLY DEFERRED` constraint triggers validate payer, split, and posting sums after all rows exist. Row-level `CHECK`, FK, uniqueness, status-transition, precision, and amount-bound constraints remain mandatory; reconciliation independently detects application or trigger defects.

### Currency, FX, and rounding

- Pin a versioned ISO-style currency metadata table with exponent and supported state; migration review is required before changing an exponent.
- Store rates as high-precision `NUMERIC`, with base/quote direction, timestamp, provider/manual source, actor, rate type, rounding mode, and rate version.
- Original amount/currency is permanent truth. Base-currency values are labeled snapshots for budgets/display and never rewrite prior postings.
- Offline cached rates enter as `estimated`; settlement cannot silently consume an unresolved rate. Manual/card/cash overrides create a new revision.
- Store split algorithm version, raw rational inputs, stable tie-break order, and resolved owed minor units. Check overflow before multiplication/division.

### Settlements, budgets, commitments, and virtual fund

- Debt simplification is a deterministic preview only; it creates no postings until participants record/confirm a settlement.
- Partial/overpayments, bank fees, and waived debt are explicit typed commands. Reversal references the original settlement and restores its postings exactly.
- Editing value already involved in settlement appends a new expense revision and marks the plan ledger `reopened` or `disputed`; it never mutates the settlement.
- Budgets are limits in plan base currency. Finance owns `cost_commitments`; booking/planning modules call its port instead of writing budget totals.
- A commitment has a unique `(plan_id, source_type, source_id, commitment_kind)` identity and state `estimated|committed|converted_to_expense|cancelled|refunded`.
- Linking a booking to an expense atomically marks its commitment converted. Budget projection selects exactly one tier per source—actual, else committed, else estimate—so booking and expense cannot double-count.
- A virtual fund is one ledger account per plan/currency. Contribution posts participant `+amount`, fund `-amount`; fund-paid expense posts fund `+amount`, consumers `-owed`.
- Fund availability is derived from typed fund flows. Custodian is metadata only; APIs and copy must state that no money is held or transferred by the app.

### Projections and reconciliation

- Maintain `participant_balances` synchronously by ledger sequence; build spend, budget, commitment, settlement, and fund summaries from committed postings/revisions.
- Projectors are idempotent on `(projection_name, ledger_transaction_id)` and cannot advance across a sequence gap.
- A scheduled reconciler recomputes per-plan/per-currency zero sums and projections from canonical history, compares checksums, and emits a zero-tolerance alert on drift.
- Repair builds shadow projection tables, records cause/audit evidence, validates totals, then atomically swaps or applies a reviewed correction. It never edits canonical postings.

## Proposed File Inventory

All paths are proposed and `[UNVERIFIED]` until the backend repository exists.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/src/beluno/modules/finance/{expenses,ledger,splits,fx,settlements,budgets,funds}/*` `[UNVERIFIED]` | Financial domain/application services | Unit/integration |
| Create | `backend/src/beluno/modules/finance/ports/cost_commitments.py` `[UNVERIFIED]` | Booking-to-budget ownership protocol | Cross-module contract |
| Create | `backend/src/beluno/contracts/finance.py` `[UNVERIFIED]` | Pydantic commands, responses, errors | OpenAPI/compatibility |
| Create | `backend/src/beluno/db/models/finance.py` `[UNVERIFIED]` | SQLAlchemy mappings and typed queries | Type/integration |
| Create | `backend/alembic/versions/000030_financial_ledger.py` `[UNVERIFIED]` | Tables, constraints, triggers, RLS SQL | Migration/security |
| Create | `backend/src/beluno/worker/tasks/reconcile_ledger.py` `[UNVERIFIED]` | Projection/invariant reconciliation | Worker/operations |
| Create | `backend/tests/property/test_finance_invariants.py` `[UNVERIFIED]` | Hypothesis money invariants | Property tests |
| Create | `backend/tests/integration/test_finance_concurrency.py` `[UNVERIFIED]` | Idempotency, locking, rollback | Integration |
| Create | `backend/tests/e2e/test_financial_lifecycle.py` `[UNVERIFIED]` | Expense-to-settlement flow | E2E |

Phase 2 module/OpenAPI registration and Phase 4 command/change handlers will be modified after their actual paths are verified. No files are deleted.

## Implementation Steps

1. Approve sign convention, account types, transaction types, revision/reversal semantics, amount bounds, and pinned currency metadata in the finance ADR.
2. Implement pure money, split, largest-remainder, FX, balance, budget-tier, fund, and settlement-preview functions with golden/property tests first.
3. Add finance tables, composite plan-scoped FKs, immutable-row guards, RLS, grants, indexes, ledger head, and deferred constraint triggers.
4. Implement the single financial command executor over Phase 4 idempotency, expected versions, ledger-head locking, audit/change/outbox, and stored responses.
5. Implement expense create/revise/void/refund with immutable payer/split snapshots and exact reversal postings.
6. Implement versioned currency metadata, FX ingest cache, manual override, stale/estimated review, explicit conversion, and provider-degradation behavior.
7. Implement per-currency balances, explainable breakdowns, deterministic debt previews, settlement recording, dispute/reopen, and reversal.
8. Implement total/category/person budgets, finance-owned cost commitments, atomic booking-to-actual conversion, refunds, and double-count prevention.
9. Implement virtual-fund contribution, withdrawal, adjustment, fund-paid expense, availability, custodian metadata, and non-custody wording.
10. Implement synchronous critical projections, worker summaries, checkpointing, shadow rebuild, drift alerting, and an audited repair command.
11. Publish REST/OpenAPI and sync schemas with stable error codes; reject incompatible clients before financial writes.
12. Run migration, concurrency, fault-injection, RLS/IDOR, load, replay, projection rebuild, and rollback rehearsals on real PostgreSQL.

## Todo

- [x] Ledger sign, account, revision, reversal, refund, and adjustment rules are approved. (Execution Decisions below; `docs/adr/0003-financial-ledger.md` and `docs/architecture/data-model.md`; postings built in `src/beluno/modules/finance/postings.py`, `tests/unit/test_finance_money.py::test_expense_postings_are_paid_minus_owed`, `::test_reversal_moves_merged_parties_to_their_survivor`)
- [x] Every supported split produces deterministic resolved minor-unit allocations. (`src/beluno/modules/finance/splits.py`, `lr-v1`; golden cases `test_one_yen_across_five_people_goes_to_the_first_captured`, `test_ten_dollars_one_cent_by_weights`, `test_itemized_bill_spreads_tax_and_tip_over_item_subtotals`; Hypothesis `test_every_method_resolves_to_the_exact_amount`, `test_largest_remainder_is_exact_and_within_one_unit_of_the_quota`)
- [x] Deferred triggers reject unbalanced payer, split, posting, and cross-plan/currency rows. (migration `000005_financial_ledger`; `tests/security/test_finance_ledger_guards.py::test_deferred_invariants_abort_the_whole_transaction`, `::test_postings_cannot_cross_plans_or_currencies`, `::test_a_reversal_stays_with_the_entry_it_reverses`)
- [x] Duplicate, lost-ack, stale-version, and concurrent commands cannot duplicate or overwrite value. (shared command runner; `test_finance_expenses.py::test_idempotent_create_replays_one_revision`, `::test_push_creates_and_conflicts_with_the_current_expense`, `test_finance_operations.py::test_concurrent_writers_serialize_on_the_ledger_head`, `test_finance_abuse_and_races.py::test_a_merge_racing_finance_writes_never_strands_money`; unique indexes allow one transaction per revision, refund, settlement, waiver, conversion, and fund movement)
- [x] FX snapshots preserve original truth and block silent cross-currency netting. (`src/beluno/modules/finance/{fx,rates}.py`; `test_finance_settlements.py::test_paying_in_another_currency_converts_explicitly`, `test_finance_expenses.py::test_foreign_currency_uses_exponents_and_labelled_base_snapshots`, `test_finance_budgets.py::test_overview_converts_labels_and_never_adds_unconverted_spend`)
- [x] Settlements, reversals, disputes, and balance explanations are complete. (`src/beluno/modules/finance/{settlements,views}.py`; `test_finance_settlements.py` lifecycle, answer, waiver, preview/explanation/journal and reopen cases; `test_finance_abuse_and_races.py::test_a_creditor_can_undo_a_payment_they_never_received`, `::test_every_creditor_can_be_answered_for`)
- [x] Commitment-to-actual transitions cannot double-count budget totals. (`src/beluno/modules/finance/{commitments,budgets}.py`; `test_finance_budgets.py::test_a_commitment_and_its_expense_count_once`, `::test_revising_an_expense_moves_its_commitment`, `test_finance_abuse_and_races.py::test_a_linked_expense_revised_with_its_commitment_keeps_it`)
- [x] Virtual fund reconciles per currency and is explicitly non-custodial. (`src/beluno/modules/finance/funds.py`, `FUND_NOTICE` in every fund response; `test_finance_fund.py::test_fund_paid_expense_and_its_void_keep_every_currency_balanced`, `::test_fund_settings_name_a_custodian_without_holding_money`, `test_finance_abuse_and_races.py::test_only_the_custodian_or_a_manager_spends_the_fund`)
- [x] All projections rebuild from canonical postings and reconciliation alerts on any drift. (balances are the only stored projection; spend, budget, settlement, and fund summaries compute on read from canonical rows. `finance.reconcile_plan`, `finance.rebuild_balances`, job `finance.reconcile_ledgers`, `scripts/finance.py`; `test_finance_operations.py::test_reconciler_reports_drift_and_the_audited_rebuild_repairs_it`, `test_finance_ledger_guards.py::test_reconciliation_reports_drift_and_rebuild_repairs_it`, `beluno.finance.ledger.drift` metric)
- [x] OpenAPI, sync events, audit, telemetry, and runbooks cover every financial command. (`openapi/openapi.json`, 18 finance commands in the catalog, seven plan-scope entities with fixtures in `tests/contract/sync-fixtures/finance-entities.json`, `test_finance_operations.py::test_every_finance_entity_flows_through_the_change_feed`, `docs/runbooks/finance-operations.md`)

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | Lost response after committed expense, then offline retry | Stored canonical response replays; one revision/transaction only |
| Critical | Two devices revise the same expense version | Exactly one commits; loser receives current snapshot/conflict metadata |
| Critical | Commit with payer/split/posting sum mismatch | Deferred trigger aborts the entire transaction; no change/audit leak |
| Critical | Booking commitment converts to linked expense | Projection counts actual once and excludes prior estimate/commitment |
| Critical | Fund contribution followed by fund-paid expense and reversal | Per-currency postings stay zero-sum; availability returns exactly |
| High | Split ¥1 across five participants and $10.01 by weights | Deterministic remainder follows captured order and algorithm version |
| High | JPY, USD, and KWD plus reversed/stale FX direction | Exponents/direction validate; unresolved estimate cannot settle silently |
| High | Partial settlement, expense edit, then settlement reversal | History remains append-only; ledger reopens and balances explain exactly |
| High | Process dies after allocations but before response/projection | Transaction rolls back or retry replays complete canonical commit |
| High | Delete/re-add removed payer or split participant | Stable participant history remains; current access rules still apply |
| Medium | Rebuild projections after induced projection drift | Canonical checksum matches rebuilt shadow; alert/audit records repair |
| Medium | Maximum amount, multiplication, or rate exceeds bounds | Stable non-retryable overflow error; no partial data or sensitive log |

## Success Criteria

- [x] Property tests prove payer, owed, posting, plan/currency zero-sum, reversal, and rounding invariants across randomized cases. (Hypothesis in `tests/unit/test_finance_money.py`; reference-model test over the public API on PostgreSQL in `tests/sync/test_finance_ledger_model.py`, checking projection, journal, and reconciliation after every step)
- [x] Real-PostgreSQL tests prove atomic revision pairs, deferred triggers, RLS, idempotent replay, and deterministic concurrency. (`tests/security/test_finance_ledger_guards.py`, `test_finance_expenses.py::test_revision_reverses_the_previous_one_and_keeps_history`, `test_finance_operations.py::test_a_command_that_fails_before_commit_leaves_nothing_behind`, `::test_concurrent_writers_serialize_on_the_ledger_head`, `test_finance_abuse_and_races.py`)
- [x] Rebuilding empty projections yields byte/logically equivalent balances, spend, budget, settlement, and fund summaries. (`finance.shadow_balances` recomputes every balance from postings and the rebuild diff is empty after every model step; the other summaries have no stored projection. Rebuild on production-shaped data is part of the Phase 8 rehearsal)
- [x] Golden cases cover JPY/KWD/USD, multiple payers, all split methods, refunds, partial settlement, manual FX, and amount bounds. (`tests/unit/test_finance_money.py` golden cases; `test_finance_expenses.py::test_split_methods_and_validation_codes`, `::test_refunds_follow_shares_and_voiding_reverses_everything`, `test_finance_settlements.py::test_settlement_lifecycle_moves_status_and_balances`)
- [x] No public command can mutate committed revisions/postings or perform unapproved cross-currency netting. (append-only triggers for every role and write guards on mutable finance rows: `test_finance_ledger_guards.py::test_history_is_append_only_for_every_role`, `::test_mutable_rows_change_only_the_way_their_commands_do`; per-currency balances and explicit `conversion` entries)
- [ ] Reconciliation, drift, lock-wait, serialization, idempotency collision, and financial failure dashboards/alerts are operational. (signals delivered: `beluno.finance.ledger_lock.wait`, `beluno.finance.ledgers.reconciled`, `beluno.finance.ledger.drift`, plus the shared command retry and failure metrics; alert thresholds are in `docs/runbooks/finance-operations.md`. Dashboard and alert provisioning moves to Phase 8 with the staging environment)
- [x] Phase 6 can create cost commitments through the finance port without querying or writing finance tables directly. (`CostCommitmentPort` / `COST_COMMITMENTS` in `src/beluno/modules/finance/commitments.py`; `test_finance_budgets.py::test_other_modules_record_costs_through_the_port`)

## Risk Assessment

- **Ledger corruption:** one invalid write poisons every balance. Centralize writes, enforce deferred constraints, append only, and reconcile continuously.
- **Rounding drift:** different clients/versions can disagree. Server resolves allocations with pinned algorithms and persists outputs.
- **Lock contention:** one plan head serializes writes. Keep transactions provider-free, index lookups, measure waits, and shard only after evidence.
- **FX ambiguity:** reversed direction or mutable rates misstates totals. Store explicit snapshots and require review before settlement.
- **Projection trust:** fast summaries can drift. Treat them as caches, alert at zero tolerance, and keep a rehearsed shadow rebuild.

## Security Considerations

- Re-authorize current participation and action policy inside the transaction; scope every FK/query by `plan_id` and reject IDOR without existence leakage.
- Runtime roles cannot directly mutate ledger tables or disable triggers/RLS; only reviewed finance procedures/services receive narrow grants.
- Never log descriptions, exact balances, notes, receipts, FX-provider secrets, idempotency payloads, or settlement metadata; audit uses safe identifiers and before/after versions.
- Apply per-actor/plan financial rate limits, amount/count bounds, anomaly signals, and step-up authentication for privileged adjustments, exports, and dispute overrides.
- Keep non-custodial fund language and prohibit payment-provider, bank-transfer, KYC, or stored-value behavior in this phase.

## Rollback and Exit Gate

Deploy schema additively, dual-read only for measured migration verification, and build new projections in shadow tables before cutover. On defects, disable affected write commands with a kill switch, keep canonical reads/export available, and roll back application code only while schema compatibility is proven. Never delete postings or reverse history as a deployment rollback; correct financial data through reviewed append-only adjustments/reversals and forward migrations.

Exit requires all hard invariants and security tests passing, exact projection rebuild on production-shaped data, commitment anti-double-count proof, a ledger-head contention/load result within SLO, reconciler/alerts/runbooks operating, and a staged rollback plus drift-repair rehearsal. Phase 7 must not consume balances, budgets, or fund totals until this gate passes.

## Execution Decisions (2026-10-06)

Agreed with the user before implementation (24 decisions); they replace the `[UNVERIFIED]` paths above. Refinements found while detailing the design are marked *(refined)*.

- **Branch.** `feat/finance-ledger` stacked on `feat/sync-kernel`; the PR targets `feat/sync-kernel` until that PR merges, then `main`. One PR, one commit per slice. Migration `000005_financial_ledger` (later slices may add `000006`).
- **Sign convention.** A posting is `paid - owed`; positive means the participant should receive money. Settlement debtor → creditor posts debtor `+x`, creditor `-x`. Every transaction sums to zero per currency.
- **Accounts.** `participant` (participant × currency) and `fund` (plan × currency) accounts in `finance.ledger_accounts`, created on first use while the ledger head is locked. Postings reference `(plan_id, account_id, currency)` through composite keys.
- **Transaction kinds.** `expense`, `expense_reversal`, `refund`, `settlement`, `settlement_reversal`, `fund_contribution`, `fund_withdrawal`, `adjustment` (subtypes `waiver`, `merge_transfer`, `fund_adjustment`, `correction`), `conversion`. A fund-paid expense is an `expense` whose payer is the fund account. Every transaction takes the next per-plan `ledger_seq`.
- **Revisions.** Revise = reversal of the current revision plus a new revision, in one database transaction; void = reversal only, permanently `voided`. `version` grows on every revision, void, and refund; revise/void/refund require the expected version. *(refined)* Voiding also reverses every refund of the expense. Refunds belong to the expense aggregate and are embedded in the expense entity.
- **Refunds.** Reference the expense; the recipient is one participant or the fund; default allocation follows the current revision's owed shares (largest remainder), explicit shares allowed. Cumulative refunds never exceed the current amount (`409 REFUND_EXCEEDS_AMOUNT`, also when a revision would go below them; a revision cannot change currency while refunds exist). *(post-review)* Nor can it change the split input, because refund credit stays allocated under the split it was made with.
- **Expense fields.** `description` 1–200 chars, `notes` ≤ 2000, `occurred_on` date (required), category from `food, lodging, transport, activities, shopping, groceries, fees, other`. Receipts arrive with attachments in a later phase.
- **Splits.** `equal` over a subset (covers exclusion/subgroup), `exact`, `percentage` (basis points summing to 10000), `shares` (integer weights 1..10^6), `itemized` (items split equally or by weights; shared extras such as tax/tip spread over item subtotals). Multiple payers give exact amounts. Largest remainder `lr-v1`: floor quotas, remaining units to the largest fractional remainders, ties to the earlier captured position. Revisions store the raw input, method, algorithm, and resolved owed minor units.
- **Currencies and bounds.** `finance.currencies(code, exponent, state, metadata_version)` seeded with active ISO 4217 codes; exponents change only by migration. `|amount| ≤ 10^12` minor units, ≤ 100 split participants, ≤ 100 items; violations are `422 AMOUNT_OUT_OF_RANGE` (never retried). *(refined)* Plan and group currencies must exist in the table, and a plan's base currency is locked once it has finance data (`409 BASE_CURRENCY_LOCKED`).
- **FX.** Sources `manual`, `estimated` (client-cached), and *(refined)* `agreed` (implied by the two amounts of a cross-currency settlement). Provider ingest is a later integration (port only). Rates are `NUMERIC(28,12)`, quote units per one base unit, half-even rounding, immutable rows in `finance.fx_snapshots`. An expense may carry a base-currency snapshot for budgets/display; it never rewrites postings. Estimated rates never back a settlement; confirming one means revising the expense with a manual rate. *(refined)* Rates are entered inline and embedded in expense/settlement entities, so there is no separate `fx_rate` sync entity.
- **No cross-currency netting.** Balances, previews, and settlements are per currency. Paying a debt in another currency records a `settlement` in the paid currency plus a `conversion` (zero-sum in each currency) in the same database transaction, with an `agreed` snapshot.
- **Settlements.** Postings are written at `settlement.record`; status `recorded → confirmed | disputed`, `disputed → confirmed`, and `reversed` (exact reversal) from any live state. *(refined)* The creditor confirms or disputes (managers act for placeholder creditors); a creditor-recorded settlement starts `confirmed`. Fees are metadata borne by the payer. Overpayment is allowed and flagged `overpaid`. Waivers are settlements of kind `waiver` (transaction `adjustment/waiver`), recorded only by the creditor (managers for placeholder creditors).
- **Debt preview.** Deterministic greedy matching per currency (largest amounts first, ties by participant id); creates nothing. When the fund still holds money the preview lists suggested fund withdrawals.
- **Ledger status.** `open | settled | reopened` plus a count of disputed settlements on the ledger head: `settled` when every participant balance is zero after at least one settlement; any later imbalance turns it `reopened`.
- **Budgets.** Plan base currency: `total`, `category`, `participant`, `daily` (one limit per day). Foreign-currency spend without a snapshot is reported as `unconverted`, never added silently.
- **Cost commitments.** Finance owns `finance.cost_commitments` (unique `(plan_id, source_type, source_id, commitment_kind)`, states `estimated|committed|converted_to_expense|cancelled|refunded`) behind a Python port for the planning module; REST exposes `manual` commitments only. Linking an expense marks the commitment converted atomically (voiding the expense restores the previous state). The budget view counts one tier per source: actual > committed > estimated.
- **Virtual fund.** Availability is minus the fund account balance per currency; a fund-paid expense, withdrawal, or adjustment that would make it negative is `409 FUND_INSUFFICIENT`. The custodian is metadata; responses and docs state that the app holds and moves no money.
- **Projections.** `finance.account_balances` updates synchronously per transaction with `last_ledger_seq`; spend, budget, and fund summaries are computed on read from canonical rows. A daily reconciler job and an audited repair script (shadow rebuild, diff, apply) never touch postings.
- **Database enforcement.** Schema `finance`. Canonical tables (revisions, payers, splits, transactions, postings, refunds, settlements' transactions, fund movements, snapshots, accounts) are INSERT-only for runtime roles, with triggers rejecting UPDATE/DELETE. `DEFERRABLE INITIALLY DEFERRED` constraint triggers check at commit: payer and split sums, zero-sum postings per currency, expense postings equal `paid - owed`, reversals mirror their source. `finance.plan_ledger_heads` is locked `FOR UPDATE` by every finance write (after the plan row) and assigns `ledger_seq`. No SECURITY DEFINER finalize function. RLS scopes every table to active participants of the plan; role rules stay in the application policy (no extra insider role guards on finance tables).
- **Merged and removed participants.** Merging a placeholder appends `adjustment/merge_transfer` moving each currency balance to the survivor in the same transaction. *(refined)* Later reversals and refunds post to the surviving participant, so merged accounts stay at zero. Removed or left participants keep accounts and history and may still settle; new revisions may only add active participants (or keep non-merged ones already in the previous revision).
- **Permissions.** View finance: every active participant (viewer and guest included), not group readers (finance entities are hidden from the `reader` access level). Create expense: owner/admin/member/guest; revise/void/refund: the creator or owner/admin. Settlements: a party records its own; owner/admin record any pair; reverse: the recorder or owner/admin. Budgets and commitments: owner/admin. Fund: contribute for oneself (managers for anyone), withdraw and settings owner/admin; ledger adjustment owner with step-up. Finance writes need `draft|planning|active|settling`; settlements and waivers also `completed`.
- **API and sync.** REST under `/v1/plans/{plan_id}/...` plus `/v1/currencies`. Commands: `expense.create|revise|void|refund`, `settlement.record|confirm|dispute|reverse|waive`, `budget.create|update|delete`, `commitment.create|update`, `fund.update|contribute|withdraw`, `ledger.adjust`. Plan-scope entities: `expense`, `settlement`, `budget`, `cost_commitment`, `fund`, `fund_movement`, and *(refined)* one `ledger` entity per plan (status, sequence, disputed count, balances embedded as values) instead of per-balance entities, so one write yields one consistent balance view. Revision history and the transaction journal are REST-only.
- **Operations.** `BELUNO_FINANCE_WRITES_ENABLED` kill switch plus `sync_disabled_commands`; rate limit 120 finance commands per minute per actor and plan; logs and audit metadata carry identifiers and versions only, never descriptions, notes, or amounts.

### Slices

| Slice | Scope |
|---|---|
| S1 Money kernel | pure currency/bounds, largest remainder `lr-v1`, split methods, payer validation, FX conversion, posting builders, debt preview, budget tiers; golden and Hypothesis tests; ADR update |
| S2 Schema | migration `000005_financial_ledger` (currencies seed, ledger heads, accounts, balances, expenses/revisions/payers/splits, transactions/postings, refunds, settlements, FX snapshots, budgets, commitments, fund), RLS, grants, immutability and deferred invariant triggers, models, database tests |
| S3 Expenses | ledger writer (plan + head locks, sequence, accounts, postings, balance projection, status), policy and permission matrix, expense create/revise/void/refund with base snapshots, currency endpoint and validation, base-currency lock, REST, commands, sync entities |
| S4 Settlements | record (with cross-currency conversion), confirm, dispute, reverse, waive; ledger view, balance explanation, journal, debt preview |
| S5 Budgets | budgets and budget view, cost commitments with port, expense linking |
| S6 Fund | fund settings, contributions, withdrawals, fund-paid expenses, `ledger.adjust`, merge transfers in claim flows |
| S7 Operations | reconciler job, repair script, metrics, kill switch, rate limit, concurrency/fault/property tests on PostgreSQL, contract fixtures, timing |
| S8 Review | independent and adversarial review, fixes, docs/ADR/plan sync, PR |

## Completion Notes (2026-10-06)

- Delivered: migration `000005_financial_ledger` (157 ISO 4217 currencies, ledger heads, accounts, balance projection, expenses with immutable revisions, payers, splits and refunds, settlements and waivers, FX snapshots, budgets, cost commitments, fund settings and movements, transactions and postings; RLS, grants, append-only and write-guard triggers, deferred invariant triggers, merge and maintenance gates); `src/beluno/modules/finance/` (money kernel, single ledger writer, expenses, settlements, budgets, commitments port, fund, merges, views, maintenance); 18 finance commands with a kill switch and a per-plan rate limit; REST under `/v1/plans/{id}/…` and `/v1/currencies`; seven plan-scope sync entities; daily `finance.reconcile_ledgers` job and `scripts/finance.py`; finance metrics; runbook `docs/runbooks/finance-operations.md`; ADR 0003 expanded.
- Verification: 529 tests passed on real PostgreSQL 16 with none skipped (unit, contract, integration, security, e2e, sync models), coverage 94 %, plus the `INTERNAL_ERROR` contract test added afterwards; ruff/format/mypy strict clean, OpenAPI exported and compatible with `feat/sync-kernel`. The finance reference model (`tests/sync/test_finance_ledger_model.py`) drives random histories through the public API and checks balances, the journal, and reconciliation after every step.
- Independent and adversarial reviews (both PASS_WITH_RISK → fixed):
  - Merges: a claim racing a finance write could deadlock or leave money on the merged participant. Both claim flows now take the plan row through `finance.lock_plan_for_merge` (plan, then ledger head, then participant rows, the same order as every finance write) before locking participants; `test_a_merge_racing_finance_writes_never_strands_money`.
  - Settlements: a manager who owed a placeholder could waive their own debt or confirm their own payment for the placeholder; waivers had no cap; a creditor could not undo a payment they never received; viewer, merged, and departed creditors could not be answered for. The creditor is now resolved through merges, managers act only when they are not the debtor, a waiver is capped by the debt between the two parties (`409 WAIVER_EXCEEDS_DEBT`), the creditor may reverse an unconfirmed settlement, and answering is its own action `plan.settlements.answer` that viewers hold. `overpaid` is computed before posting.
  - Fund: any expense creator could pay from the fund; fund-paid expenses now need a manager or the custodian. Fund settings rejected stale versions but accepted a blind overwrite; now `428` without `If-Match`.
  - Database: mutable finance rows could be rewritten freely by anyone holding the runtime role. Write guards now let only state columns change, require the version to grow, and keep voided and reversed rows terminal; unique indexes allow one settlement, waiver, and conversion transaction per settlement; a reversal must be filed under the entry it reverses; reconciliation reports `merged_balance`.
  - Projections: the ledger stayed `settled` after every settlement was reversed (now `open`); a balance rebuild bumped no ledger version and published no change, so devices kept the drifted balance.
  - Smaller: converted commitments could not be edited; commitment edits computed the base snapshot from half-applied fields; N+1 queries in expense lists and the budget overview; control characters (C0 except newline and tab, DEL) accepted in text; SQL parameters could reach database error logs (`hide_parameters`); unhandled errors returned a bare 500 (now an `INTERNAL_ERROR` problem that logs only the error type); fund adjustments recorded a second change row; dead code removed.
- Accepted and documented risks: client-chosen IDs reveal existence through `409 ALREADY_EXISTS` (as in Phase 4); members may record expenses that put debt on others (approved decision: the creator or a manager corrects them); an insider holding the runtime role can still self-merge within the `000003` guards; accounts created by the merge gate get UUIDv4 IDs; merges and port calls ignore the finance kill switch because they keep money consistent; claim flows do not retry deadlocks (lock ordering removes the known cause; a rare invite-revoke versus merge ordering remains). Unhandled errors are still re-raised to the server log and Sentry with their message, which for PostgreSQL errors can include a failing row; scrubbing that is a platform follow-up.
- Deviations from the Execution Decisions: the database now carries finance write guards and SECURITY DEFINER gates for merges and maintenance (not a finalize path; the application still writes every entry); creditors may reverse settlements they never confirmed; answering a settlement is a separate action; a ledger with zero balances and no live settlement is `open`; spending from the fund needs a manager or the custodian.
- Left for later phases: dashboard and alert provisioning, ledger-head contention and load results on staging, and the exit-gate rehearsals (rebuild on production-shaped data, staged rollback, drift repair) in Phase 8; provider FX ingest, receipts and attachments, and finance exports in later integration work; Phase 6 callers of the commitment port; a final legal retention policy for financial history.
- Post-merge review fixes (2026-10-06):
  - Refunds: a revision that changed the split left refund credit with people who no longer shared the cost (a participant dropped from the bill stayed owed their refund share). While refunds are in effect a revision now keeps the split input as well as the currency (`409 INVALID_STATE_TRANSITION`); the amount may still change. `test_finance_expenses.py::test_a_refunded_expense_keeps_its_split`.
  - Time zone: sessions did not pin `TimeZone`, so with a non-UTC server or role default a row serialized as `Z` in its write response but with the local offset in reads and sync pulls. Pooled connections now add `-c timezone=UTC` to the DSN's options. `test_database_sessions.py`.
  - Claimed guests: the account that claimed a guest could not change the guest's expenses or reverse its settlements. Migration `000006_merged_guest_authorship` adds the definer check `iam.user_merged_into_actor`. `test_guest_claim.py::test_an_account_that_claims_a_guest_keeps_changing_the_guests_records`.
  - Phase 6 note: `CostCommitmentPort` callers must lock the plan row (`load_plan(..., for_update=True)`) before calling the port, which takes the ledger head, to keep the plan → ledger head lock order.
