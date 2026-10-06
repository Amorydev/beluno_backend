---
phase: 5
title: "Financial Ledger"
status: pending
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

- [ ] Ledger sign, account, revision, reversal, refund, and adjustment rules are approved.
- [ ] Every supported split produces deterministic resolved minor-unit allocations.
- [ ] Deferred triggers reject unbalanced payer, split, posting, and cross-plan/currency rows.
- [ ] Duplicate, lost-ack, stale-version, and concurrent commands cannot duplicate or overwrite value.
- [ ] FX snapshots preserve original truth and block silent cross-currency netting.
- [ ] Settlements, reversals, disputes, and balance explanations are complete.
- [ ] Commitment-to-actual transitions cannot double-count budget totals.
- [ ] Virtual fund reconciles per currency and is explicitly non-custodial.
- [ ] All projections rebuild from canonical postings and reconciliation alerts on any drift.
- [ ] OpenAPI, sync events, audit, telemetry, and runbooks cover every financial command.

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

- [ ] Property tests prove payer, owed, posting, plan/currency zero-sum, reversal, and rounding invariants across randomized cases.
- [ ] Real-PostgreSQL tests prove atomic revision pairs, deferred triggers, RLS, idempotent replay, and deterministic concurrency.
- [ ] Rebuilding empty projections yields byte/logically equivalent balances, spend, budget, settlement, and fund summaries.
- [ ] Golden cases cover JPY/KWD/USD, multiple payers, all split methods, refunds, partial settlement, manual FX, and amount bounds.
- [ ] No public command can mutate committed revisions/postings or perform unapproved cross-currency netting.
- [ ] Reconciliation, drift, lock-wait, serialization, idempotency collision, and financial failure dashboards/alerts are operational.
- [ ] Phase 6 can create cost commitments through the finance port without querying or writing finance tables directly.

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
