# Code review: Tasks + Packing slice (feat/planning-tasks-packing, uncommitted)

## Scope
- Files: alembic/versions/000014_coordination.py, src/beluno/db/models/coordination.py, src/beluno/modules/planning/{tasks,packing}.py, contracts/planning.py, api/{planning_projection,projection}.py, api/commands/planning.py, api/routers/planning.py, activity/events.py, testkit/{database,tenants}.py, tests (tasks_packing, plan_purge, idor_sweep), docs.
- LOC: ~1.4k new + ~900 changed (excl. openapi.json).
- Verification run: targeted suites 11 passed (live PG :54331); ruff, ruff format, mypy strict clean; OpenAPI vs HEAD: 8 paths added, 0 paths/schemas changed or removed. Purge function diffed against 000013: only additions (private-item notifications, 3 coordination DELETEs before polls/items/bookings).
- Scratch probes (scratchpad, not in repo) confirmed H1, H2, M1 below.
- Note: I ran `scripts/export_openapi.py`, which rewrote openapi/openapi.json; diffstat stayed identical (5626+/2926-), so content unchanged.

## Overall
Service, guards, and RLS are consistent with prior slices; flush ordering is right (validation queries before mutation, version bump before flush); FOR UPDATE policies equal SELECT policies so hidden rows are 404 and visible-but-not-yours are 403; share emits user-scope delete + plan-scope upsert at the same version; loaders and pagers check scope; purge is complete. The defects are all in identity/participation lifecycle: private items live in a *user* scope but are gated by *plan participation* and keyed by a raw user id, and nothing in claim/merge/leave/account-deletion knows about them.

## Critical
None.

## High

**H1. A guest who signs in to an existing account loses their private packing list.**
`packing.py:75` stores `owner_user_id = guest.id`; RLS (`000014:235-245`) and pager (`planning_projection.py` `page_packing`, user scope filters `owner_user_id == scope_id`) only match the exact id. `transfer_guest_participations` (`modules/plans/guest_claims.py:26`) relinks or merges the participant row but never touches `coordination.packing_items` or `template_applications`.
Probe: guest creates private "Meds" + shared "Tent", claims existing Google account (relink path, no merge) -> `GET /packing` shows only Tent; tick on Meds -> 404; account's user-scope pull has no packing_item. Same for the merge path. Items are stranded until purge.
Fix: in the claim transaction, call a new SECURITY DEFINER (fixed search_path, REVOKE PUBLIC, grant api_runtime) e.g. `coordination.adopt_guest_private_items(guest, target)` that sets `owner_user_id = target`, bumps version, deletes guest `template_applications` that would collide on (plan, target, template) and re-owns the rest, and appends user-scope upserts for `target` (guest sessions are revoked, so no guest-scope delete needed). Gate it on `iam.users.merged_into_user_id = target` / relinked participant. Add tests for both relink and merge.

**H2. Leaving or being removed leaves private items on the device forever; snapshot and change feed disagree.**
RLS SELECT for private items requires `plans.actor_is_active_participant` (`000014:235-237`). `_leave`/`remove_participant` (`modules/plans/participants.py:326,351`) write no user-scope `packing_item` change.
Probe: Dan pulls user scope (socks upsert), leaves -> incremental pull returns only `plan_access` upsert, no packing delete; fresh snapshot returns no socks. Rejoin is the mirror image: items become visible again in snapshots but incremental pulls never re-deliver them. This violates the pull contract (feed converges to snapshot) in the user scope, which is new territory: before this slice nothing in a user scope was gated by plan participation.
Fix (simplest): make private-item SELECT `owner_user_id = iam.actor_id()` without the active-participant gate (keep the gate on INSERT/UPDATE and in the service). The owner keeps reading their own list after leaving; purge already sends the delete. Alternative: emit user-scope deletes on leave/remove/reject and upserts on rejoin/approve — more seams, easy to miss one. Either way add a leave/rejoin sync test.

## Medium

**M1. A merged assignee can no longer move the task.** `tasks.py:112-116` compares `access.participant.id == task.assignee_participant_id`; guard `000014:162-166` requires `id = OLD.assignee_participant_id AND user_id = actor AND active`. After a guest merges into an existing member (or an existing participant claims a placeholder), the assignee row is `merged` with `user_id` of the guest/none.
Probe: task assigned to guest Gia; Gia merges into member Mo -> Mo `POST /status` -> 403 "Only the person who added it...". (Owner re-save with unchanged merged assignee still 200, good.) Same pattern as bookings `traveler_ids`; `bringer_participant_id` is display-only so less harmful.
Fix: either rewrite `tasks.assignee_participant_id` / `packing_items.bringer_participant_id` to `merged_into_participant_id` inside the merge transaction (DEFINER, version bump, plan-scope change rows), or accept "assignee or a row merged into the actor's row" in both service and guard.

**M2. Account deletion keeps private packing items (free text, `health` category) with no reader.** `modules/account_deletion.py` scrubs profile, crews, participant names, but not `coordination.packing_items` where `owner_user_id = actor`. Nobody can see or delete them (RLS owner-only) until plan purge, possibly never if the plan is not deleted. Retention row added in `docs/contracts/retention-matrix.md` ("live with their plan") is the author's wording, not a user decision.
Fix: tombstone and blank names (or hard-delete) the actor's private items and template_applications during account deletion via a DEFINER; update the retention row.

**M3. `apply_template` is N+1.** `packing.py:196-199` calls `create_item` per draft: each does `planning_access` (load_plan + participant), SAVEPOINT, INSERT, RELEASE. Up to 100 items -> ~500 round trips in one transaction holding the template_applications unique lock (a concurrent apply waits the whole time).
Fix: authorize once, validate bringers once (one `IN` query), `add_all` + single flush, then record each item.

## Low

- **L1. PUT silently reopens done tasks.** `TaskRequest.status` defaults to `"open"` (`contracts/planning.py` TaskRequest); a PUT that omits status wipes `completed_at`/`completed_by` (`tasks.py:192`). The test at `test_planning_tasks_packing.py:108-113` encodes this. An older/offline client editing only the title of a done task reopens it. Decide now (changing a default later is a contract change): make `status` required on PUT, or "omitted = keep".
- **L2. `completed_by_user_id` not tied to status in DB.** CHECK covers only `completed_at` (`000014:77`). Assignee path of the guard (`000014:158-161`) and INSERT let an insider set any `completed_by_user_id`, or a non-null value while open. Add `CHECK ((status = 'done') = (completed_by_user_id IS NOT NULL))` and require `NEW.completed_by_user_id = actor` when status changes to done in the assignee branch.
- **L3. Template can never be re-applied once its items are gone.** `packing.py:181-195`: deleting every item of an applied template makes later applies return `[]` forever. Also a private template item that was shared does not count for the shared list's application (duplicates on shared apply). Product question, see below.
- **L4. Creating a task already `done` emits no `task.completed`.** `create_task` (`tasks.py:89`) passes no activity; update/set_status do. `full_tenant` uses this path.
- **L5. Packing PUT/DELETE query the row before authorizing the plan.** `packing.py:98,150`: on a hangout they return 404 instead of 409 `NOT_AVAILABLE_FOR_HANGOUT`; `set_packed`/`share_item` rely only on RLS for private ownership (no explicit owner check in service, unlike `_require_editor`). Behaviour is safe (404), just inconsistent with tasks.

## Edge cases checked (OK)
- apply_template race: second insert blocks on the unique index, gets IntegrityError after commit, READ COMMITTED re-read sees committed items; rollback of first lets second proceed. Catch-all IntegrityError is acceptable (only the application insert is in the savepoint).
- Unchanged links (item/booking/assignee/bringer) skip re-validation, so tombstoned targets and departed people don't block re-saves (tested for item).
- Bringer: rejected on private items, must be active (placeholders allowed).
- Read-only plan states: every write path goes through CONTRIBUTE/RESPOND/MANAGE, all limited to EDITABLE_PLAN_STATES.
- Hangouts: create paths 409 (tested).
- Private items never reach plan scope: `_record` routes by visibility; `_in_scope` and `page_packing` filter by visibility + scope id.
- Purge: private deletes go to every owner (incl. those who left); FK order tasks/packing before items, bookings, participants.
- DB guards: identities fixed, `version = OLD+1`, tombstones final, shared->private refused, private owner-only; DB lets a demoted creator edit (service stricter) — same as bookings, not a finding.

## Test quality
No vacuous asserts; DB guard test uses `pytest.raises` outside `transaction()` correctly. Gaps: no claim/merge/leave/rejoin coverage (H1, H2, M1 would have been caught), no concurrent template apply, no test that a non-owner cannot share, IDOR sweep only exercises a shared packing item (`full_tenant` returns the template item id), not a victim's private item.

## Recommended actions
1. H1 + M1: one DEFINER-backed "adopt merged guest" step in the claim/merge transaction covering private items, template applications, assignees, bringers; tests for relink and merge.
2. H2: drop the participation gate from private-item SELECT (or emit lifecycle changes); add leave/rejoin pull test.
3. M2: account deletion clears private items.
4. M3: batch apply_template.
5. L1/L2 before the contract ships.

## Unresolved questions
- L3: should re-applying a template after its items were all deleted add them again?
- H2: after leaving a trip, should a person keep read access to their private list (my recommendation), or should it disappear from their devices?

Status: DONE_WITH_CONCERNS
Summary: Service/guard/RLS/sync mechanics are sound and checks pass, but private packing items and task assignees break across guest claim/merge, leaving the trip, and account deletion (3 confirmed by live probes).
Concerns/Blockers: H1 and H2 should be fixed before merge; they are user-visible data loss / sync divergence.
