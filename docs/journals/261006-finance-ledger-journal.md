# 2026-10-06 — Financial ledger

## What changed

- Migration `000005_financial_ledger`: currencies seeded from ISO 4217 with pinned exponents, per-plan ledger heads, participant and fund accounts, balance projection, immutable expense revisions with payers and splits, refunds, settlements and waivers, FX snapshots, budgets, cost commitments, fund settings and movements, and the journal (transactions and postings). Canonical rows are append-only for every role; deferred constraint triggers verify sums and posting shapes at commit.
- A single ledger writer: plan row lock, ledger head lock, contiguous `ledger_seq`, accounts on first use, postings and balances together, fund availability, ledger status (`open|settled|reopened`), one `ledger` sync change per write.
- Expenses (create, revise as reversal plus new revision, void, refund), settlements (record with cross-currency conversion, confirm, dispute, reverse, waive), budgets and the no-double-count budget view, finance-owned cost commitments behind a port, the virtual fund, owner-only step-up adjustments, and merge transfers for placeholder and guest claims.
- 18 finance commands in the shared catalog with a kill switch and a per-plan rate limit; REST under `/v1/plans/{id}/…` and `/v1/currencies`; seven plan-scope sync entities hidden from group readers.
- Daily reconciliation job, audited balance rebuild CLI, finance metrics, runbook.

## Lessons

- Writing the invariants as commit-time triggers that recompute each entry from its source rows (not just "sums to zero") made the database an independent check of the application: an expense whose postings balance but disagree with its splits is rejected.
- Merged participants were the subtle case: reversing an old entry would have put money back on a merged placeholder. Reversals and refunds now resolve to the surviving participant, and the trigger compares after the same resolution.
- RLS limits finance rows to active participants, so a merge running as a claimed guest cannot write the ledger. A narrow SECURITY DEFINER gate that only moves a merged row's full balances is safer than widening the policies.
- A reference-model property test over the public API (expenses, revisions, voids, refunds, settlements, fund flows, merges) is cheap with the money kernel reused for expectations, and it checks the database projection, the journal, and reconciliation at once.

## Decisions taken with the user

Stacked branch; sign convention `paid - owed`; participant and fund accounts per currency; the nine transaction kinds; revision by reversal; `lr-v1` splits; pinned ISO 4217 exponents with a `10^12` bound; manual/estimated/agreed FX snapshots with no silent netting; settlements posted on record and confirmed by the creditor; budgets in base currency with an `unconverted` bucket; finance-owned commitments; a non-custodial fund; synchronous balances with a daily reconciler; INSERT-only canonical tables with deferred triggers; merge transfers; finance private to participants; settlements allowed after completion.

## Open

Provider-backed FX ingest (later integration phase), receipts and attachments, bookings and itinerary calling the commitment port (Phase 6), finance exports, staging load and contention measurements and dashboard provisioning (Phase 8), and a final legal retention policy for financial history.
