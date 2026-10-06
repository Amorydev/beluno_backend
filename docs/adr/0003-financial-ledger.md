# ADR 0003: Financial Ledger

**Status:** Accepted (expanded 2026-10-06)

Financial history is append-only. Expenses have immutable revisions; changes,
voids, refunds, settlements, and fund activity create balanced postings rather
than overwriting history. Projections are rebuildable caches.

## Decisions

- **Money.** Amounts are `BIGINT` minor units of a currency whose exponent is
  pinned in `finance.currencies` (seeded from ISO 4217; an exponent changes only
  by a reviewed migration). Every amount is between 1 and `10^12` minor units;
  floating point never touches money.
- **Sign convention.** A posting is `paid - owed`: positive means the party
  should receive money. A settlement from debtor to creditor posts the debtor
  `+x` and the creditor `-x`; a fund contribution posts the contributor `+x` and
  the fund `-x`; a withdrawal is the opposite. Every transaction sums to zero in
  each currency, so every plan does too.
- **Accounts.** One account per participant and currency and one fund account
  per plan and currency. Participants keep their accounts and history after
  leaving or removal. Merging a placeholder moves its balances to the survivor
  with an explicit `merge_transfer`, and later reversals or refunds post to the
  survivor, so a merged account stays at zero.
- **Transactions.** `expense`, `expense_reversal`, `refund`, `settlement`,
  `settlement_reversal`, `fund_contribution`, `fund_withdrawal`, `conversion`,
  and `adjustment` (`waiver`, `merge_transfer`, `fund_adjustment`,
  `correction`). Each takes the next per-plan `ledger_seq` under the plan's
  ledger head lock.
- **Revisions.** Editing an expense appends the exact reversal of its current
  revision and a new revision with full postings in the same database
  transaction; voiding appends only reversals (of the revision and of every
  refund). The previous rows are never updated or deleted. While refunds are in
  effect a revision keeps the currency and the split input (the amount may
  change, never below the refunds): refund credit was allocated under that split
  and must stay with the people who share the cost.
- **Splits.** Equal (over any subset), exact, percentage (basis points), shares
  (integer weights), and itemized splits reduce to integer weights resolved by
  the largest-remainder algorithm `lr-v1`: floor quotas, then one unit each to
  the largest fractional remainders, ties to the earlier captured position.
  Revisions store the raw input, method, algorithm version, and resolved owed
  minor units.
- **Currencies.** Balances, debt previews, and settlements are per currency;
  nothing is netted across currencies silently. FX snapshots (`manual`,
  `estimated`, or `agreed` from both amounts of a cross-currency settlement) are
  immutable `NUMERIC(28,12)` rows with an explicit direction and half-even
  rounding. Base-currency values are labelled snapshots for budgets and display
  and never rewrite postings. Paying a debt in another currency appends a
  settlement in the paid currency plus a `conversion` that is zero-sum in each
  currency.
- **Budgets and commitments.** Budgets are limits in the plan's base currency.
  Finance owns cost commitments; other modules change them only through the
  finance port. Each source counts in exactly one tier (actual, else committed,
  else estimated), so a booking and its expense never double-count.
- **Virtual fund.** The fund is a ledger account, not money held by Beluno: the
  custodian is metadata, and the app never holds, moves, or stores value.
- **Enforcement.** Runtime roles may only insert canonical rows (revisions,
  payers, splits, transactions, postings, refunds, fund movements, snapshots);
  triggers reject updates and deletes. Deferred constraint triggers check at
  commit that payers and splits add up, that postings sum to zero per currency,
  that expense postings equal `paid - owed`, and that reversals mirror their
  source. Write guards on the mutable rows (expenses, settlements, budgets,
  commitments, fund settings, ledger heads, balances) let only state columns
  change, require the version to grow, and keep voided and reversed rows
  terminal; unique indexes allow one ledger transaction per revision, refund,
  settlement, waiver, conversion, and fund movement. Merges move balances only
  through narrow SECURITY DEFINER gates. Balances are a synchronous projection,
  checked daily by a reconciler (which also flags money left on merged
  participants) and repairable only by an audited rebuild that never edits
  postings.
- **Settlements.** Postings are written when a payment is recorded; the
  creditor (whoever the creditor's money now belongs to after merges, managers
  for placeholder or departed creditors) confirms or disputes it and may
  reverse one they never confirmed. A waiver forgives at most the debt that
  exists between the two parties and is never given by the debtor.

## Consequences

- Corrections are new entries; history explains every balance exactly.
- One plan's finance writes serialize on its ledger head; measure before
  sharding a hot plan.
- A plan's base currency is fixed once it has financial records.
