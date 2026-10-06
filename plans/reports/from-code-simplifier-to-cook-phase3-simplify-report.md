# Phase 3 simplify report (code-simplifier -> cook)

Date: 2026-10-06. Scope: identity, groups, plans backend code. Behavior-preserving only.

## Changes (12 files)

| File | Change |
|---|---|
| `src/beluno/modules/plans/service.py` | Added `_load_for_change(ctx, plan_id, action, expected_version)` (same as the groups helper: lock, authorize, version check, in the same order). It replaces 4 copies in `update_plan`, `change_state`, `schedule_deletion`, `restore_plan`. The duplicate-seed check now uses a list plus a set instead of a set plus `sum(...)`. |
| `src/beluno/modules/groups/service.py` | Named constants `LIVE_STATES` (ACTIVE, INVITED) and `FALLBACK_DISPLAY_NAME`. They replace 4 inline state tuples/lists and 2 `"Beluno member"` literals. |
| `src/beluno/modules/plans/invites.py` | `redeem` now uses `participants.LIVE_STATES` instead of an identical inline tuple. Removed stray parentheses in the `_check_email_binding` comparison. |
| `src/beluno/modules/plans/series.py`, `changes.py` | Removed `_bump_series(ctx, series)`, which had the same body as `changes.bump`. Added `PlanSeries` to `bump`'s type union. |
| `src/beluno/modules/iam/users.py` | Moved the "verified email unless another account has it" logic into `_unclaimed_verified_email`. `create_registered_user` and `upgrade_guest` both call it, at the same point as before. |
| `src/beluno/modules/iam/sessions.py` | `issue_access_token` (tuple return, only used inside this module) became `_issue_tokens(..., refresh_token=)`, which returns `IssuedTokens`. This removes 3 copies of the `IssuedTokens(...)` construction. Token minting still happens before `record_mutation`, as before. |
| `src/beluno/modules/invite_links.py` | `issue_invite_token` reuses `token_digest`. |
| `src/beluno/modules/plans/guest_claims.py` | Docstring: "live participant" changed to "non-merged participant". The query only excludes MERGED rows, and "live" means ACTIVE/PENDING elsewhere (`participants.LIVE_STATES`). |
| `src/beluno/modules/plans/travel.py` | `_apply_segment`: replaced awkward tuple-unpacking assignments with plain assignments. |
| `src/beluno/api/routers/auth.py` | Google and Apple handlers now share `_sign_in_external(provider, ...)`. Route functions, paths and signatures are unchanged, so operation IDs are unchanged. |
| `src/beluno/api/routers/invites.py` | `redeem_invite` computes `client_subject(request)` once. The conditional `plan=` argument is built from a precomputed `plan_view`. |

## Invariants checked (grep counts, baseline -> now)
- Unchanged: `record_mutation` 14, `record_plan_change` 10, `record_participant_change` 20, `record_invite_change` 4, `record_group_membership` 10, `_record_group` 6, `_record_series` 4, `_record_user_change` 6, `act_as` 8, `set_invite_context` 2, `with_for_update` 27, `begin_nested` 4, `on_conflict_do_nothing` 1 (users.py, pre-existing, not on an RLS-insert path changed here), `returning` 0.
- `load_plan`/`require_plan`/`version_conflict()` each went down by 3 (4 call sites folded into 1 helper).
- `modules/iam` gained no imports. No new files or dependencies.

## Verification (exact outputs)
- `uv run ruff check .` -> `All checks passed!`
- `uv run ruff format --check .` -> `133 files already formatted`
- `uv run mypy src` -> `Success: no issues found in 85 source files`
- `BELUNO_TEST_ADMIN_DATABASE_URL=... uv run pytest -q` -> `266 passed, 1 warning in 7.68s` (the warning is pre-existing: short JWT HMAC test key)
- `uv run python scripts/export_openapi.py` -> no output; `diff -r` against a pre-change copy of `openapi/` shows no difference
- `uv run pytest -q tests/contract/test_openapi_snapshot.py` -> `1 passed in 0.13s`

## Considered, deliberately left as-is
- `participants.transfer_ownership` has the same load/version pattern. Reusing `service._load_for_change` would create a circular import (service imports participants).
- `version += 1; updated_at = now` repeats in users.py and travel.py. These are 2-line blocks on models outside the `bump` union. Low value.
- Two flows look similar but differ in fields or order, so they were not merged: `invitations.preview_invite`/`redeem_invite`, and `groups/invites.redeem` branching.
- `invites.py` router: `MANAGE_ERRORS` and `PUBLIC_ERRORS` are identical values but name different intents.

## Unresolved questions
1. `SeriesChanges.clear_start_time` is never set to True. `SeriesSplitRequest` has no way to clear the start time, and an explicit `local_start_time: null` currently means "keep the series time". Is the field dead, or is a contract field missing? Left untouched (removing it or wiring it up would change behavior or the API).
