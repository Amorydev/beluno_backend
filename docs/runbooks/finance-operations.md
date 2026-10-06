# Runbook: Finance Ledger Operations

The ledger is append-only and self-checking: deferred constraint triggers verify
every entry at commit, and a daily job reconciles every plan. Balances are a
projection that can always be rebuilt from postings. Never edit or delete
postings, revisions, or settlements; corrections are new entries.

## Signals

| Metric | Meaning | Alert |
|---|---|---|
| `beluno.finance.ledger.drift` (attribute `problem`) | Reconciliation findings: `unbalanced_transaction`, `balance_drift`, `sequence_gap`, `merged_balance` | any value above zero pages on-call (zero tolerance) |
| `beluno.finance.ledgers.reconciled` | Plans checked by the daily job | missing for more than 26 hours |
| `beluno.finance.ledger_lock.wait` (ms) | Time a finance command waited for its plan's ledger head | p95 above 200 ms for 15 minutes |
| `beluno.commands` (`command=expense.*`, `settlement.*`, ...) | Finance command outcomes | sustained `retry_later` or 5xx |
| `beluno.command.retries` | Deadlock/serialization retries | sudden growth on finance commands |

Logs carry plan and account identifiers only; amounts, descriptions, and notes
are never logged.

## Market Rates

The `finance.ingest_market_rates` job runs daily to fetch market rates for offline
estimates. Rates are stored in `finance.market_rates` (reference table, append-only,
worker-only insert). The API serves the latest rates via `GET /v1/fx/rates?base=XXX`
with `estimate_only: true`; the ledger never reads them. A `RateProvider` port
exists for plugging in a rate source; currently a no-op adapter (no provider chosen;
ECB lacks VND).

## Consolidation

A consolidation freezes one rate per foreign currency with open balances and moves
everything into the base currency. Only the latest consolidation can be reversed
via `POST /ledger/consolidations/{id}/reverse` with `If-Match`; reversal is
possible only while no payment or waiver still in effect was recorded after it
(409 `CONSOLIDATION_SETTLED`); reverse those first if the group agrees. A
reversal posts one `conversion_reversal`. While a consolidation is active and
the ledger is not settled, the base currency cannot change (409
`CONSOLIDATION_OPEN`).

## Kill switch

Set `BELUNO_FINANCE_WRITES_ENABLED=false` and restart the API. Every finance
command answers `503 FEATURE_DISABLED` (push reports `retry`, so clients keep
their outbox); reads, the journal, explanations, and sync pull stay available.
The handshake reports `finance_writes_enabled: false`. Commands covered by the
kill switch: `expense.*`, `settlement.*`, `budget.*`, `commitment.*`, `fund.*`,
`ledger.*`, and `plan.change_base_currency`. Single commands can be disabled with
`BELUNO_SYNC_DISABLED_COMMANDS`.

## Drift response

1. Confirm: `uv run python scripts/finance.py reconcile --plan PLAN_ID` (worker role).
2. `unbalanced_transaction` or `sequence_gap` means canonical history is wrong —
   this should be impossible past the commit-time triggers. Turn on the kill
   switch, preserve a database snapshot, and escalate; do not repair by hand.
3. `balance_drift` means only the projection disagrees with postings. Dry run:
   `uv run python scripts/finance.py rebuild PLAN_ID` lists each account with its
   recorded and rebuilt balance.
4. Apply: `uv run python scripts/finance.py rebuild PLAN_ID --apply --operator NAME`.
   The rebuild locks the plan's ledger head, rewrites only `account_balances`,
   and records a `finance.balances_rebuilt` audit event naming the operator.
5. Re-run step 1 (expect no output) and record the cause in the incident.
6. `merged_balance` means a participant was merged without the merge transfer
   (for example by a manual data fix outside the claim flows). History is intact
   but the money sits on an account nobody can settle. Escalate; the fix is a
   reviewed forward data fix that calls `finance.transfer_merged_balances` for
   that participant. Never edit balances or postings directly.

## Correcting money

- A wrong expense is revised or voided by its creator or a plan manager; a wrong
  settlement is reversed by its recorder or a manager, or by its creditor while
  they have not confirmed it. Both append exact reversals.
- Anything else (for example cash handed over outside any settlement) is a
  privileged adjustment: the plan owner, after a recent sign-in, posts
  `POST /v1/plans/{id}/ledger/adjustments` with a memo and entries that sum to
  zero. It is audited (`finance.ledger_adjusted`).

## Restore

A database restore must bump every sync scope generation (see
`sync-operations.md`) and then run the reconciliation job before reopening
finance writes.

## Non-custody

The virtual fund records what participants pooled with a custodian. Beluno
holds, moves, and stores no money; support copy and API responses say so.
