# Documentation Updates: People, Activity, Account Deletion

**Branch:** `feat/people-activity-account`
**Date:** 2026-10-07

## Files Changed

### Created
- **`docs/adr/0009-activity-feed-and-account-deletion.md`** — Architecture Decision Record documenting activity feed design (append-only schema, typed summaries, plan and user scopes, 180-day retention) and account deletion workflow (step-up required, ownership constraint, participant renaming, crew removal, identity and session revocation, profile scrubbing).

### Updated

**`docs/contracts/sync-protocol.md`**
- Added `activity_event` entity to both `user:{id}` scope (user-scope events) and `plan:{id}` scope (plan-scope events).
- Documented ledger entity carries `suggestions` (deterministic settlement suggestions per currency, same computation as `GET /ledger/settlement-preview`, never netted across plans or currencies).

**`docs/contracts/error-catalog.md`**
- Clarified `OWNER_TRANSFER_REQUIRED` error: applies both to leaving a plan and deleting an account while owning a plan with other active participants.

**`docs/contracts/retention-matrix.md`**
- Added `activity.events`: ≥180 days retention via daily `activity.purge_events` job, runs in batches alongside change-log compaction.

**`docs/architecture/data-model.md`**
- Updated `iam.users` documentation: status field now includes `deleted` state; deleted accounts hold no email, locale, timezone, or default currency.
- Updated `iam.sessions` documentation: revoke_reason now includes `account_deleted`.
- Added new **Activity** section: `activity.events` schema (id, scope_type, scope_id, plan_id, actor_user_id, type, entity_type, entity_id, summary, occurred_at); visibility rules via RLS; retention mechanism.

**`docs/contracts/permission-matrix.md`**
- Added prose clarification: ownership constraint applies to both `plan.leave` and account deletion; ownership must be transferred before account deletion if owning plans with other active participants.

**`docs/runbooks/sync-operations.md`**
- Added activity events to retention section (180-day retention using `sync_change_retention_days`).
- Documented daily `activity.purge_events` job removes older events in batches.
- Expanded caution: do not delete activity rows by hand.

**`README.md`**
- Expanded "Sync and reliability" section: noted activity feed tracks typed events (never free text) in plan and user scopes, synced and retained 180 days.
- Added new "Account management" section: documented `DELETE /v1/me` workflow, ownership constraint, and link to ADR 0009.

## Facts Verified Against Code

- **Activity schema:** `alembic/versions/000009_activity_and_deletion.py` lines 48–150 — confirmed table structure, RLS policy `events_select`, SECURITY DEFINER gate `activity.append_events`, append-only trigger, purge function signature.
- **ActivityType enum:** `src/beluno/modules/activity/events.py` lines 21–46 — confirmed 24 event types for release 1 (expense, payment, waiver, budget, base_currency, ledger consolidation, kitty, member, guest, plan).
- **Summary keys:** `src/beluno/modules/activity/events.py` lines 51–88 — confirmed `SUMMARY_KEYS` frozenset (27 typed keys, no free text allowed).
- **Ledger suggestions:** `src/beluno/contracts/finance.py` line 418 — confirmed `suggestions: list[SettlementPreviewResponse]` field in `LedgerResponse`.
- **Retention configuration:** `src/beluno/config.py` — confirmed default `sync_change_retention_days = 180`.
- **Purge activity job:** `src/beluno/modules/sync_audit/maintenance.py` lines 51–68 — confirmed `purge_activity()` uses same retention cutoff.
- **Activity purge task:** `src/beluno/worker/tasks.py` lines 118–129 — confirmed periodic task `activity.purge_events`, cron `47 3 * * *`, name `activity.purge_events`.
- **Account deletion:** `src/beluno/modules/account_deletion.py` — confirmed `delete_account()` checks for other active participants, raises `OWNER_TRANSFER_REQUIRED`, renames participants to "Former member", marks active ones `left`, runs `people.forget_member()`, revokes sessions with reason `account_deleted`, scrubs profile with status `deleted`.
- **DELETE /v1/me endpoint:** `src/beluno/api/routers/me.py` lines 57–73 — confirmed endpoint exists, docstring describes step-up requirement and ownership constraint.
- **Error code:** `src/beluno/modules/account_deletion.py` line 69 — confirmed error code string is `OWNER_TRANSFER_REQUIRED` (matching the one for `plan.leave`).
- **Sessions revoke reason:** `alembic/versions/000009_activity_and_deletion.py` line 160 — confirmed `account_deleted` added to `sessions_revoked_reason_check` constraint.

## Unresolved Questions

None. All documented behavior has been verified against current code in the branch.

---

**Status:** DONE
**Summary:** Created ADR 0009 and updated 7 existing docs to cover activity feed (sync entity, typed summaries, 180-day retention, plan and user scopes) and account deletion (step-up, ownership constraint, participant lifecycle, crew handling, session revocation, profile scrubbing).
**Concerns/Blockers:** None.
