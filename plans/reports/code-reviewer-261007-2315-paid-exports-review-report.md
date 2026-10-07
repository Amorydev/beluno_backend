# Paid exports (accounting CSV, PDF trip report) review

Branch `feat/pdf-reports`, uncommitted. Reviewer probes are in the session scratchpad (`test_probe_accounting.py`, `test_probe_pdf.py`, `pdf_stress.py`, `loop_lag2.py`).

## Scope
- Files: src/beluno/api/{accounting,trip_report,paid_exports,exports}.py, api/routers/exports.py, modules/billing.py, contracts/errors.py, tests/integration/test_paid_exports.py, docs/contracts/{billing,error-catalog}.md, README.md, openapi.json, pyproject/uv.lock
- Gates: `ruff check` clean, `ruff format --check` clean, `mypy src` clean. test_paid_exports + test_exports: 9 passed. test_billing + tests/unit: 170 passed. Regenerated openapi.json is byte-identical to the committed one.

## Verdict
Not ready to merge: 1 Critical, 1 High. Gate, RLS scope, and formula escaping are sound. The free CSV/JSON exports did not regress.

## Critical

**C1. Owner ledger adjustments are missing, so `net` does not add up to the balances.** `accounting.py:57-62`
- `ledger_lines` only reads expenses, settlements, fund movements, and consolidations. `funds.adjust_ledger` (`funds.py:299`, kind `adjustment`, subtype `correction` or `fund_adjustment`) posts to the ledger, but sync has no entity for it, so the CSV never shows it.
- Proven twice:
  - `exercise_money_and_members` (owner adjustment of 250 EUR between Ann and Dan): the CSV has no EUR rows, while the ledger has Ann +250 and Dan −250.
  - A second probe with a correction (Ann +250 / Dat −250) and a fund adjustment (Kitty +100 / Ann −100): the per-account differences are exactly those two entries.
- Impact: the core promise of the paid feature (and of README and billing.md) is silently false for any trip with an owner correction. Users see wrong totals with no hint why.
- Every other case adds up exactly in the probes:
  - multi-payer and revised expenses
  - a fund-paid expense
  - refunds to the kitty, including custom refund shares
  - cross-currency payment with a fee and overpayment
  - disputed payment
  - waiver
  - contribution and withdrawal
  - consolidation, both active and reversed
  - placeholder merge, then voiding a pre-merge expense
  - base-currency change
  - person who left
- Fix:
  - Inside the request transaction, read `ledger_transactions` (kind `adjustment`, subtype `correction` or `fund_adjustment`) with their postings and accounts. Emit `entry=adjustment`, `reference_id=transaction id`, and the date from `created_at`.
  - Leave the memo out: today only the owner's POST response returns it, and the activity feed deliberately omits it.
  - Exclude `merge_transfer` (`holder()` already covers it) and `waiver` (the settlement row already covers it).
  - Stronger alternative: build lines from `ledger_postings` joined to their source entities, so the sum holds by construction whenever a new entry kind appears.
  - Add `exercise_money_and_members` to `test_paid_exports.py` and assert the sums. That would have caught this.

## High

**H1. The PDF render time has no bound and holds the GIL.** `paid_exports.py:123-176`, `trip_report.py:174-192`
- Every active expense becomes a table row, and there is no cap on expenses per plan.
- Measured (fpdf2 2.8.9, M-series): 1,000 rows take about 5 s (54 pages); 5,000 rows take 29 s (264 pages, 22 MB peak).
- Four concurrent 1,000-row renders in `anyio.to_thread` add 35 ms median and 128 ms max lag to the event loop, because rendering is pure Python and holds the GIL. Every request on the worker slows down.
- The rate limit is 20/h per user, but every member of a big trip can trigger it, and a 30 s response also risks proxy timeouts.
- Fix (pick one or more):
  - Cap the expenses table (for example the newest 500, plus a note pointing to the accounting CSV).
  - Put a process-wide `anyio.CapacityLimiter(1-2)` on PDF renders.
  - Render in a subprocess or Procrastinate job.
  - Cache the parsed fonts. `add_font` re-parses the 620 KB TTFs on every call, about 0.15 s of the 0.2 s baseline.

## Medium

**M1. Thai, CJK, Arabic, Hebrew, and emoji text vanishes from the PDF without a trace.** `trip_report.py:30-32`
- Probes:
  - A Thai description (`ข้าวผัด`) and an emoji-only one (`🍜🍜`) print as empty cells.
  - An Arabic name prints as a blank person row.
  - fpdf logging is silenced at ERROR, so nothing is reported.
- For trips to Bangkok or Tokyo this makes rows ambiguous. Your Noto Sans decision stands; the gaps are a consequence of it.
- Fix within that decision:
  - Vendor Noto Sans Thai, Arabic, and Hebrew (small) as fallbacks via `pdf.set_fallback_fonts`. CJK would cost about 16 MB, or could use a subset.
  - At minimum, swap unsupported characters for a visible placeholder (`?`) so a cell is never blank.
- The docs mention only emoji.

**M2. The gate runs after the full sync snapshot is loaded.** `paid_exports.py:50-52`, `93-97`
- `plan_entities` pages every entity type before `require_unlocked`: activity events, planning, media, and every expense.
- An unpaid trip therefore pays the full read cost and holds the pooled connection, then gets a 403. Hangout PDFs likewise load everything before the 409.
- Fix: `scope_access` → read `Plan.type` → `require_unlocked` / hangout check, then `plan_entities`.
- Also: the PDF only needs plan, plan_participant, expense, settlement, fund_movement, consolidation, and media. Load just those instead of `activity_event` and the planning types.

**M3. `rate_to_base` is mislabelled after a base-currency change.** `accounting.py:118`, `paid_exports.py:57,81`
- The rate comes from `revision.base`, i.e. relative to the revision's own `base.currency` (the base at entry time). The row labels it with the plan's current `base_currency`.
- Scenario: a JPY expense valued against USD, then the base changes to EUR. The row says `rate_to_base=0.0067, base_currency=EUR`, and an accountant multiplying gets wrong numbers.
- Fix: emit `revision["base"]["currency"]` for expense and refund rows, and `consolidation["base_currency"]` for consolidation rows.

**M4. Rows identify people only by display name.** `paid_exports.py:75`
- Duplicate display names (two "Minh") and a participant literally named "Kitty" make rows impossible to reconcile per person.
- Fix: add a `person_id` column (the participant id; empty for the kitty).

## Low
- **L1.** `accounting.py:139,195`: refund and consolidation dates are the UTC `created_at[:10]`, while expenses use the local `occurred_on`. Near midnight, a refund can sort before its expense.
- **L2.** `accounting.py:18,48-55`: `MAX_MERGES=32` duplicates `ledger.MAX_MERGE_DEPTH=16`, and on a chain that is too long or cyclic it silently returns an intermediate holder. Reuse the ledger's rule, or raise.
- **L3.** `paid_exports.py:93-99`: plan entities, `get_recap`, and `ledger_snapshot` are separate reads under READ COMMITTED. An expense committed between them can make "Spent", the people table, and the expense list disagree in one PDF.
- **L4.** `trip_report.py:32`: setting the global `fpdf` logger level at import is a process-wide side effect. Prefer a filter scoped to the render, or keep it but document it.
- **L5.** Test gaps:
  - none of: adjustments, merges, cross-currency payments, the receipt mark (`media` kind receipt and state ready), viewer or guest download
  - no test against a big plan
- **L6.** The phase file says the sums are "tested with refunds, payments, waivers, the kitty, and consolidations". Correct as written, but it does not cover owner adjustments (C1).
- **L7.** The accounting CSV keeps only the debt-currency payment of a cross-currency settlement. `paid` / `paid_currency` and `fee_minor` are dropped. The sums are still correct; this is informational.

## Acceptance criteria
1. Sums: **fail** (C1). Every other ledger path verified as summing correctly; no mis-signed entries.
2. Gate and leaks: pass.
   - `billing.plan_unlock` (SECURITY DEFINER, active participant, unrevoked pass or active owner's Pro) cannot be bypassed: plan type is immutable.
   - Viewers and guests get MEMBER sync level and `VIEW_FINANCE`, so their exports match what they sync (viewer probe returned 200).
   - Private packing, secrets, and the adjustment memo are not emitted.
3. Formula injection: pass. Description, person, and notes go through `spreadsheet_text`; `clean_text` strips leading Unicode whitespace; category is an enum; numbers are left unescaped, correctly.
4. PDF: no external I/O, and it renders in a thread after the transaction. Long words, long titles, zero-width, bidi and control characters, and empty plans all render; JPY (0 decimals) and KWD (3) format correctly. **Fails** on huge plans (H1) and non-Latin text (M1).
5. Regression: pass. test_exports is green and the renamed helpers behave the same; `names_of` adding `None→Kitty` does not change `_expense_row`.

## Recommended order
C1 (plus the exercise test) → H1 → M2 → M3 → M4 → M1 → Lows.

## Unresolved questions
1. M1: vendor the extra Noto scripts (Thai, Arabic, Hebrew small; CJK large), or replace unsupported characters with a placeholder for now?
2. H1: should the PDF cap its expense table (and at what number), or must it always list every expense, which would mean moving rendering to a job or subprocess?
3. C1: should the adjustment memo appear in the accounting CSV? Today only the owner sees it.
