# Code review: people, activity feed, account deletion

Branch `feat/people-activity-account`, commits since `feat/money-alignment`:
a2273fc ledger suggestions, 7bf27e1 activity feed, 569cc4e account deletion
(`git diff feat/money-alignment...HEAD`, 49 files, +1968/-107).

## Scope
- Files: migration `000009_activity_and_deletion.py`, `modules/activity/events.py`, `sync_audit/recorder.py`,
  `context.py`, `plans/changes.py` + call sites (`plans/*`, `finance/*`, `iam/users.py`), `api/projection.py`,
  `finance/views.py`, `account_deletion.py`, `routers/me.py`, `worker/tasks.py`, `sync_audit/maintenance.py`, tests.
- Plan: `phase-03-people-activity-account.md` (Execution Decisions treated as user decisions).
- Live probes: scratch pytest files against the disposable cluster on :54331 (not committed).

## Overall assessment
Activity plumbing is sound: events ride the same buffer as audit/change rows, savepoint rollback drops them,
replays write none, every call-site summary is free of text. Ledger suggestions are correct. Account deletion
has real gaps: a merged guest's name survives deletion, the owner check races invite joins, and the
`append_events` gate is looser than its test claims. Gates green.

## Gates (re-run by reviewer)
- `ruff check`, `ruff format --check`, `mypy src` (strict): clean.
- Full suite on live PG: **513 passed, 0 skipped**, coverage 95%.
- `scripts/export_openapi.py` output equals committed `openapi/openapi.json`.

## Critical
None.

## High

### H1. Deleted account still shows the person's guest-era name (merged guest rows and retired guest user)
`src/beluno/modules/account_deletion.py:50-58,75-76` only touches rows with `user_id = user.id`. A guest who
claimed an existing account with merge consent leaves its row `merged` with `user_id = <retired guest id>`
(`src/beluno/modules/plans/guest_claims.py:79-83`), and the retired guest `iam.users` row keeps its
`display_name` (`users.retire_merged_guest`, `merged_into_user_id = account`). Deletion never follows
`merged_into_user_id`.
Probe (verified): guest "An Nguyen (guest)" merges into account An, An deletes the account -> owner's
participant list = `[('Linh','active'), ('Former member','left'), ('An Nguyen (guest)','merged')]`;
`iam.users` guest row = `('An Nguyen (guest)', 'disabled', <An's id>)`.
Impact: breaks the success criterion "shows as Former member everywhere"; personal data stays visible to other
members after an erasure request. Common path (guest joins, later signs in to an existing account).
Fix: in `delete_account`, also scrub `iam.users WHERE merged_into_user_id = actor` and rename participant rows
of those user IDs. The participant write guard rejects `OLD.user_id <> actor`, so do it in the
SECURITY DEFINER gate (`iam.forget_actor_credentials` or a new one). Add a test.

## Medium

### M1. Owner-alone check races invite joins; a new member gets stranded in an orphaned plan
`account_deletion.py:64-74` locks owned plans `FOR UPDATE` "so nobody joins in between", but redeem reads the
plan unlocked (`src/beluno/modules/plans/invites.py:264-267`, checks `_accepts_new_people` at :464) and only
locks the invite row. The participant INSERT waits on the FK `KEY SHARE` until deletion commits, then
succeeds without re-checking. A `left` person rejoining (`_join` -> `reactivate`, UPDATE, no FK check) does not
wait at all.
Probe (verified, lock held only to fix the interleaving): DELETE /v1/me -> 204, concurrent redeem -> 200; the
plan ends `deletion_scheduled_at IS NOT NULL` with `[('Former member','owner','active'), ('Late','member','active')]`.
Impact: the joiner sits in a plan whose owner is deleted. Nobody can manage it, and only the owner can restore
it, though `_schedule_plan_deletion` says "restorable until purged". The Phase 4 purge then erases their data.
Fix: in `delete_account`, lock and revoke the active invites of each solo plan before counting. Redeem already
serializes on the invite row and re-checks its state, which closes both the insert and the rejoin paths.
Alternative: a DB check that a participant can become active only while `deletion_scheduled_at IS NULL`.

### M2. `activity.append_events` admits removed, pending, and merged rows; forged events carry arbitrary text
`alembic/versions/000009_activity_and_deletion.py:102` gates plan events on `plans.actor_has_participant_row`
(any state), and inserts `summary`, `type`, `entity_id`, and `occurred_at` as given (:110-121).
Probe (verified, api_runtime connection, actor = removed member, then actor = pending applicant): both
`append_events` calls succeed. The owner's synced feed shows
`('expense.added', {'description': 'Pay me at evil.example …'})` twice. `occurred_at = 2099-01-01` also puts
the rows beyond the purge.
The test `tests/integration/test_activity_feed.py:122` is named `..._cannot_write_into_it` but only
exercises an outsider with no row. The removed-member claim is untested and false.
Impact: under the project's insider model (write guards hold RLS against insiders), people the plan already
removed, or never approved, can inject free text into active members' feeds. That undoes the "no free text"
guarantee at the DB boundary.
Fix (order matters):
1. Stop emitting events for non-active rows (see L1). Otherwise sign-in claims of left/removed guest rows
   would fail a tighter gate.
2. Gate on `plans.actor_is_active_participant(scope_id)`, or on `type = 'member.left'` when the actor's row is
   `left`.
3. Optionally reject summary keys outside the allowed set in SQL, and clamp `occurred_at` to
   `transaction_timestamp()`.
4. Test the removed-member and pending-applicant cases.
The migration header ("people who just left included") also understates what the gate admits.

### M3. Owners whose co-participants are only placeholders or guests can never delete their account
`account_deletion.py:83-93` counts every other active row, placeholders and guests included.
`transfer_ownership` refuses non-registered targets (`src/beluno/modules/plans/participants.py:417-418`), and
the CHECK requires owners to be users.
Probe (verified): trip with owner + placeholder "Mom" -> DELETE /v1/me `409 OWNER_TRANSFER_REQUIRED`, and the
transfer to Mom -> `409 "The new owner must be a registered participant"`. Owner + guest gives the same 409.
Impact: the advice in the error can't be followed. The only way out is removing every placeholder or guest
first. For guests that means taking away their access, which the solo-plan path would do anyway.
Needs a product call. Either count only registered active users as blockers (placeholder-only plans then
count as solo), or return a distinct code that tells the owner to remove people.

### M4. Guests can delete their account only within 10 minutes of joining
`account_deletion.py:47` requires a fresh step-up (`auth_step_up_max_age_seconds` = 600). Guests have no
identity to re-authenticate with; signing in upgrades the account instead.
Probe (verified): a guest deletes within the window -> 204; after aging `authenticated_at` -> `403 STEP_UP_REQUIRED`.
No guest deletion test exists.
Impact: an invite-minted guest account becomes undeletable shortly after creation (store-policy motivation of
this feature). Product call: exempt guests from step-up (a guest session is the only credential), or accept and
document.

## Low

### L1. Feed reports changes about people members never saw
`participant_activity` (`src/beluno/modules/plans/changes.py:54-77`) keys on the action, not on the previous
state:
- A pending applicant who deletes the account (pending -> left at `account_deletion.py:109-113`) or withdraws
  produces `member.left`.
- A manager removing a pending applicant (`remove_participant` accepts `LIVE_STATES`) produces `member.removed`.
- A guest claim of a left or removed row (`guest_claims.py:93-102`) produces `guest.linked` in a plan the
  person is no longer in.

Probe (verified): a plain member's feed gets `member.left` for an applicant who never appeared (no
`member.joined`).
Fix: emit only when the row was active before the change. This also lets M2 tighten the gate.

### L2. `DELETE /v1/me` replay with an Idempotency-Key returns 401, not the stored 204
The session is revoked by the first call. `load_current_actor` rejects the replay before the stored outcome is
read. Probe: `204` then `401`. Clients must treat 401 after a sent DELETE as success. Document it in the
endpoint description, or drop the idempotency header from this route.

### L3. Lost update on participant rows during deletion
`account_deletion.py:50-58` loads rows without `FOR UPDATE`, and `bump` increments the stale in-memory version.
A manager removing the person or changing their role concurrently gets overwritten (`removed` -> `left`), and
both writes end with the same `version`. Sync clients that compare versions can keep the wrong state. Fix: add
`.with_for_update()` to the rows select (plan_id, id order already set).

### L4. Personal data that deletion does not reach (not visible to others)
- `iam.sessions.device_label/platform/app_version` on revoked sessions are never purged.
- `sync_audit.operations.response_body` holds e.g. profile responses with email for 180 days.
- Tombstoned crews keep `name` and `member_user_ids`.

Decide with the Phase 4 retention work.

### L5. Owner ledger adjustments are not in the feed
`funds.adjust_ledger` (`src/beluno/modules/finance/funds.py:303-310`) only audits. A privileged correction
changes balances with no feed entry. The plan's release-1 list omits it, so confirm that's intended.

## Edge cases checked, no defect
- Atomicity: `ctx.savepoint` drops `pending_activity` with audit and change rows (`context.py:102-111`). The
  runner retries with a fresh context. The refused-command test asserts no orphan events.
- Replay and push retry: the stored outcome returns before the handler. `test_idempotency` asserts a single
  `plan.created`. Push reuses the runner path.
- `act_as` switching: the gate reads the final actor at flush. Sign-in merge and upgrade flows end as the
  account or guest that owns every buffered scope (`sign_in.py:95`, `guest_claims.py:117-126`). No worker path
  emits activity, so a NULL actor never reaches the gate.
- Noise: the owner's own join at creation and pending join requests emit nothing. Approval emits
  `member.joined`. Role and capability edits pass explicit items.
- Visibility: RLS `events_select` = active participant or own user scope. Plan pull requires an active row.
  Member-level visibility now includes `activity_event`, and no event references manager-only invites.
- Summaries: every call site checked. Only amounts, closed-set currency/category/scope/role/capability/state
  values, field names, ISO dates, and IDs appear. Descriptions, notes, `FundCount.note`, names, and codes are
  excluded. `ActivityItem` rejects unknown keys at runtime.
- Retention: `purge_events` batches by `occurred_at` with an index, and the worker has EXECUTE only. Devices
  keep already-synced events until they resync, since purge emits no tombstones. Change rows for purged events
  read as `delete`, which is harmless.
- Sync sequencing: one extra change row per event, appended right after its entity's row. The updated pull
  tests still check the watermark and late-change intent (`test_sync_pull.py` pages stop at 51, late change at 52).
- Ledger suggestions (c): `suggest_settlements` is the preview's code moved into `ledger_snapshot`, and the
  preview now filters the same list. Balance, tolerance (`ledger_settings`), and base changes (`ledger.touch()`
  in `base_currency.py`) all bump the ledger version. The tests assert `ledger.suggestions == preview`
  before and after a tolerance change.
- Migration (d): the header has all five sections. All five new functions are `REVOKE … FROM PUBLIC` with
  `search_path = pg_catalog, pg_temp`. Grants are minimal: `append_events` to api, `purge_events` to the
  worker, no INSERT/DELETE on the table, and an append-only trigger. The replaced CHECKs keep the
  000002/000007 predicates plus the new values. `platform-runtime-grants.sql` includes `activity`. Re-adding the
  CHECKs scans under ACCESS EXCLUSIVE, which the header acknowledges (fine before launch).
- Deletion basics are verified by tests and probes: step-up, 409, sessions revoked, identities and challenges
  deleted (by email, before the scrub), profile scrubbed, crews (own tombstoned; others' via the gate, with
  change rows to their owners), and money still settleable with `reconcile_plan` clean.
- Invites the deleted user created in other plans stay active and plan-owned, the same as when an admin
  leaves. Under M1's fix, solo plans' invites would be revoked.
- Solo plan with pending or left others: scheduled for deletion per the user decision. Pending requests stay
  unanswerable and left members' balances go with the Phase 4 purge (see questions).

## Test quality
- Phantom claim: `test_removed_people_lose_the_feed_and_cannot_write_into_it` (M2).
- Missing: deletion with merged guest rows (H1), with pending rows (L1), by a guest (M4), placeholder- or
  guest-only owner (M3), email-challenge removal, idempotent replay (L2), concurrent join (M1).
- Updated sync and e2e tests keep their original assertions (contiguity, watermark, replay, counts now
  include `feed`). No vacuous asserts found.

## Recommended actions
1. H1: scrub retired merged guests and their participant rows inside a DEFINER gate, and add a test.
2. M1: revoke and lock solo plans' active invites before the ownership count.
3. L1 then M2: emit only for previously active rows, tighten `append_events`, and fix the test.
4. M3/M4: product decisions, then align the error code or step-up rule.
5. L3: lock the participant rows. L2: document the replay behavior.

## Plan follow-ups
- Requirements implemented: ledger suggestions, the feed (all 27 types emitted), DELETE /v1/me.
- Success criteria still open: "Former member everywhere" (H1) and "never leak to removed participants"
  (holds for reads, writes per M2).
- Leave phase status `in-progress` until H1/M1/M2 are resolved.

## Unresolved questions
1. M3: should placeholders and guests block an owner's deletion?
2. M4: should guests be exempt from step-up when deleting?
3. In solo-owned plans, `left` members with non-zero balances lose that ledger at the Phase 4 purge. Is that
   acceptable under the "owns alone" decision, or should balances block it?
4. L5: should owner ledger adjustments appear in the feed?
