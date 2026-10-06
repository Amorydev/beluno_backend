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
- A plan's base currency can change at a frozen rate (see Update 2026-10-06).

## Update (2026-10-06): money alignment

- **Hangouts** keep expenses, refunds, settlements, and waivers; budgets, cost
  commitments, and every fund path are for trips (`409 NOT_AVAILABLE_FOR_HANGOUT`).
- **Revisions** may carry `occurred_at` and an IANA `occurred_timezone`
  (`occurred_on` is the local date there), and record their `source` (`http` or
  `sync`), the device's `client_created_at`, and the session's `device_label`.
  Split method `adjustment` gives signed per-person adjustments in minor units
  and shares the rest equally with `lr-v1` (a negative rest is shared the same
  way, negated); a split that leaves anyone owing below zero is rejected. A
  revision whose only payer is its only sharer is reported as `personal`.
- **Settings** on the ledger head: `count_personal_spend` (budget view) and
  `settle_tolerance_minor`. Base-currency balances within the tolerance count
  as settled for the status and stay out of suggested transfers; postings stay
  exact. Participants confirm the ledger at its current sequence
  (`ledger_confirmations`); any new entry makes confirmations stale.
- **Kitty**: an optional per-member target, and cash counts against what the
  ledger expects; counts post nothing.
- **Market rates** are reference data for offline estimates; no entry reads them.
- **Consolidation** freezes one rate per foreign currency with open balances
  (the kitty must be empty in it) and appends one `conversion` that moves every
  such balance into the base currency, allocated with the largest-remainder rule
  on each side so it stays zero-sum per currency. Immutable lines record each
  person's move and the database checks the entry posts exactly them. The latest
  consolidation can be undone by an exact `conversion_reversal` until a payment
  or waiver still in effect is recorded after it.
- **Base currency** changes at a frozen old-to-new rate recorded as a numbered,
  immutable change. Revisions and commitments keep the change number they were
  valued at; base values read through every later change, and amounts already in
  today's base count as they are. Budget limits and the settle tolerance are
  re-denominated; postings and original amounts never change. A change waits
  while a consolidation is active and the ledger is not settled
  (`409 CONSOLIDATION_OPEN`).
