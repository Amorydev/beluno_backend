# Documentation Update Report: Money Alignment

**Date:** 2026-10-06  
**Branch:** feat/money-alignment  
**Baseline:** fix/finance-refund-split-lock-and-utc-sessions

## Summary

Updated 8 documentation files to reflect money-alignment work: expense revisions now carry time/timezone and revision origin; ledger settings (count personal spend, settle tolerance) and confirmations added; fund counts, market rates, consolidation, and changeable base currency fully documented.

## Files Changed

### 1. docs/adr/0003-financial-ledger.md
- **Change:** Appended "Update (2026-10-06): Money Alignment" section to Consequences
- **Content:** Comprehensive summary of new features: occurred_at/occurred_timezone, split method adjustment, base_change_number, ledger settings, confirmations, fund targets, fund counts, market rates, consolidation with reversal limits, and base currency changes
- **Verification:** All feature names verified against code; migration header referenced

### 2. docs/contracts/sync-protocol.md
- **Change 1:** Updated scope table to include `fund_count` and `consolidation` in finance entities
- **Change 2:** Updated entity description section to detail what ledger and expense entities now carry:
  - Expense: occurred time/timezone, split method, revision origin
  - Ledger: money settings, confirmations per participant
- **Verification:** Entity names match `FINANCE_TYPES` in finance_projection.py

### 3. docs/contracts/error-catalog.md
- **Change 1:** Removed `BASE_CURRENCY_LOCKED` (409 error, no longer valid)
- **Change 2:** Added 5 new errors with HTTP status and descriptions:
  - `NOT_AVAILABLE_FOR_HANGOUT` (409): budgets, planned costs, kitty target, conversions are trips-only
  - `LEDGER_CHANGED` (409): ledger sequence changed; review and confirm again
  - `FUND_NOT_EMPTY` (409): kitty holds money in a currency to consolidate
  - `CONSOLIDATION_SETTLED` (409): payments recorded after consolidation; reverse them or keep balances
  - `CONSOLIDATION_OPEN` (409): active consolidation blocks base currency change
- **Verification:** All error codes and descriptions verified against code (errors.py, ledger_settings.py, consolidation.py, base_currency.py)

### 4. docs/contracts/permission-matrix.md (prose only)
- **Change:** Expanded "Row rules on top of the table" section to cover:
  - Fund counts: custodian or manager with fund.manage
  - Consolidation: manager action only (plan.ledger.consolidate)
  - Ledger configuration and confirmation: configure (manager), confirm (all roles)
  - Base currency change: manager action, blocked by open consolidation
- **Verification:** Rules match policy.py (PLAN_RULES dictionary)

### 5. docs/architecture/data-model.md
- **Change:** Updated Finance section to document:
  - plan_ledger_heads: added money settings and base_change_count
  - expense_revisions: added revision origin, occurred time/timezone, base_change_number
  - ledger_transactions: expanded transaction kinds (added conversion, conversion_reversal)
  - Added 4 new tables: ledger_confirmations, fund_counts, base_currency_changes, consolidations/rates/lines
  - Added market_rates reference table (worker-only insert)
  - Updated cost_commitments: now carries base_change_number
- **Verification:** All table names and columns match 000008_money_alignment.py migration

### 6. docs/runbooks/finance-operations.md
- **Change 1:** Added "Market Rates" section: daily job, offline estimates, no-op adapter, GET /v1/fx/rates?base=XXX with estimate_only flag
- **Change 2:** Added "Consolidation" section: reversal limits (latest only, before settlements after it), CONSOLIDATION_SETTLED error
- **Change 3:** Expanded "Kill switch" section to list all commands covered by finance disable
- **Verification:** Market rates worker job and RateProvider port verified in code; consolidation limits verified in consolidation.py

### 7. docs/adr/0008-trip-first-realignment.md
- **Change:** Added to "Contract breaks" section: `plan.update` no longer accepts `base_currency` (use POST /v1/plans/{id}/base-currency)
- **Verification:** Verified plan.update schema does not accept base_currency parameter; change command exists

### 8. README.md
- **Change:** Updated Finance section (one sentence addition):
  - Added mention of changeable base currency and consolidation
  - Added reference to /v1/fx/rates endpoint
- **Verification:** Endpoints verified against routers/finance.py

## Verification Checklist

- [x] Removed all references to `BASE_CURRENCY_LOCKED` from docs and README (grep: 0 results)
- [x] All endpoint paths match code (`/ledger/settings`, `/ledger/confirm`, `/ledger/consolidations`, `/fund/counts`, `/v1/fx/rates`, `/v1/plans/{id}/base-currency`)
- [x] All command names verified: `ledger.configure`, `ledger.confirm`, `fund.count`, `ledger.consolidate`, `ledger.reverse_consolidation`, `plan.change_base_currency`
- [x] All sync entities match FINANCE_TYPES: ledger, expense, settlement, budget, cost_commitment, fund, fund_movement, fund_count, consolidation
- [x] All error codes verified in source: NOT_AVAILABLE_FOR_HANGOUT, LEDGER_CHANGED, FUND_NOT_EMPTY, CONSOLIDATION_SETTLED, CONSOLIDATION_OPEN
- [x] Authorization rules match policy.py (PLAN_RULES for CONFIGURE_LEDGER, CONFIRM_LEDGER, CONSOLIDATE_LEDGER, CHANGE_BASE_CURRENCY, MANAGE_FUND)
- [x] Transaction kinds verified: added conversion, conversion_reversal to existing kinds
- [x] Migration 000008 header claims match documented features (no discrepancies)

## Notes

- All documentation updates preserve conciseness per task requirements
- Permission matrix table rows were not modified (parsed by test)
- journals/ and previous contracts/permission-matrix.md table rows untouched as specified
- No code or test files modified
- All statements verified against code before documentation

## Unresolved Questions

None. All facts verified against source code.
