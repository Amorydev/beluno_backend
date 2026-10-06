# Code Review: trip-first realignment (Phase 1)

Range reviewed: `b77a53f..8a27fd3` (73b4930, b1afe17, 7f0f943, 8a27fd3) on
`fix/finance-refund-split-lock-and-utc-sessions`. Docs commit 131bd8d (landed during the review)
was only checked for the rules it states.

## Scope

- Files: 100 changed (+6.5k / -13.1k). Focus: `000007`, policy/access, plans service/participants/duplication/invites, crews, sync scopes/projection/push, OpenAPI gate.
- Gates I ran myself on live PG 18 (`localhost:54330`):
  - `ruff check`, `ruff format --check`, `mypy src` (strict): clean.
  - `coverage run -m pytest`: **452 passed, 0 skipped**, 95% coverage.
  - The OpenAPI export matches `openapi/openapi.json`.
  - `check_openapi_compatibility.py main → head`: every break it detects is in `accepted-breaks.json`.
- Throwaway probes (session scratchpad, nothing added to the repo) confirmed every finding below except Low-5 and Low-6.

## Overall assessment

The code is solid and follows the repo patterns: `open_context`, `load_plan`/`require_plan`, `record_mutation`, versioned commands, RLS-safe inserts, and savepoints for ID races. The RLS rewrites are correct and the removals are complete: grep finds nothing left in `src/`, and the `pg_catalog` test passes. There is one migration defect that blocks a deploy, one authorization hole (an admin can change the owner's settings), and one gap in the capability lifecycle (grants outlive the role and the membership they were given for). There are no data leaks and no crew privacy holes.

---

## High

### H1. `000007` fails and rolls back when a `plans.extend_series_horizons` job is still queued
`alembic/versions/000007_trip_first_realignment.py:175-182`

```sql
DELETE FROM jobs.procrastinate_jobs
WHERE task_name = 'plans.extend_series_horizons' AND status = 'todo';
```

- Procrastinate's `BEFORE DELETE` trigger `procrastinate_trigger_delete_jobs_v1` runs `procrastinate_unlink_periodic_defers_v1()`. That function does `UPDATE procrastinate_periodic_defers` without a schema name and relies on `search_path=jobs,public`.
- The bootstrap sets that search path. Alembic runs as `migrator` with the default search path, and the role has no `search_path` setting.
- Reproduced:
  1. Upgrade to `000006`.
  2. Apply the Procrastinate schema.
  3. Insert one `todo` row for the series task.
  4. `upgrade head` fails with `UndefinedTable: relation "procrastinate_periodic_defers" does not exist`, and the whole upgrade rolls back.
- When it happens: any environment whose worker served the `plans` queue and still holds a pending row (the worker stopped before picking it up, or a failed run waiting for retry). With no matching rows the trigger never fires, so the existing migration test (scratch DB without the `jobs` schema) misses it.
- This breaks the success criterion "Migration 000007 upgrades a database seeded with … series …" on a realistic database.
- Fix: run `SET LOCAL search_path = jobs, public, pg_catalog;` (or `PERFORM set_config('search_path', 'jobs, pg_catalog', true)` inside the DO block) before the DELETE. Alternatively drop the DELETE: workers no longer subscribe to the `plans` queue and an unknown task only fails harmlessly.
- Add a migration test that bootstraps the job schema and seeds a queued series job.

---

## Medium

### M1. Admins can change the owner's and other admins' `default_share` (target-role rule bypassed)
`src/beluno/modules/plans/participants.py:265-290`

- `update_participant` checks `can_manage_participant` only when `role` changes. `default_share`, and `capabilities: []`, can be written on any active row as long as the caller holds `CHANGE_PARTICIPANT_ROLE` (owner or admin).
- The DB guard returns `NEW` for every admin, so it does not stop this either.
- Probe results:
  - admin PATCHes the owner's row `{"default_share": 10000}` → **200**;
  - admin PATCHes another admin's row `{"default_share": 1}` → **200**;
  - admin PATCHes the owner's row `{"role": "member"}` → 403, as it should.
- This contradicts the documented target rule in `docs/contracts/permission-matrix.md` ("admins manage only members, viewers, and guests").
- Failure scenario: an admin sets the owner's default weight to 100× and their own to 0.01×. Every later shares-split that clients pre-fill from `default_share` then charges the owner more.
- Fix: for every manager field (role, default_share, capabilities), require `can_manage_participant(_role(access), PlanRole(target.role))` unless `own_row`. Optionally add the same target-role rule to `participant_write_guard` for the `default_share`/`capabilities` columns.

### M2. Capability grants survive demotion, removal, and leaving, and silently come back
Locations:
- `participants.py:128-145` (`reactivate`)
- `participants.py:186-192` (re-add)
- `participants.py:270-276` (role change)
- `invites.py:300-308` (rejoin by invite)

Probe on a member holding `expenses.manage`:
- demoted to viewer → response still lists `capabilities: ["expenses.manage"]`;
- promoted back to member → the grant is active again;
- removed, then re-added by POST `/participants` → `["expenses.manage"]`, and the member can void the owner's expense (**200**);
- leaves, then rejoins through a plain member invite → `["expenses.manage"]`.

The plan states "Managers grant capabilities to registered active members". Today a grant outlives both the role and the membership it was given for:
- A manager who removes someone and later re-adds them, or who demotes and later re-promotes them, re-arms finance management without seeing it.
- A viewer's row advertises a capability the policy ignores, so the client renders wrong UI.
- The risk register also asked for "admin-demotion cases" in the security suite; none cover this path.

Fix:
- Clear `capabilities` in `reactivate()`, in `_leave`/`remove_participant`/rejection, and whenever the role moves off `member`.
- Optionally add a DB check: `CHECK (capabilities = '{}' OR (role = 'member' AND identity_kind = 'user' AND access_state = 'active'))`. It also stops admin insiders from putting capabilities on a placeholder row before a bearer claim.

---

## Low

1. **Empty participant update bumps the version.** `contracts/plans.py:227-233` accepts a body such as `{"role": null}` because `model_fields_set` is not empty. The probe got 200, the version went 3→4, and an audit row was written with `changed: ""`. The same applies to `{"avatar_color": null}` on one's own row. Fix: reject the body when every sent value is null, or skip `bump`/`record` when `actions` is empty.
2. **"Start with crew" does not skip and report guests; the whole plan creation fails.** `service.py:227-234` → `participants.py:148-156`. A seed naming a guest (crews may contain guests saved from a plan) returns `409 GUEST_NOT_ALLOWED`; the probe confirmed it. A person who stopped sharing a plan returns 404. Both abort the plan create. The plan asks for guests to be "skipped and reported". The `addable` flag is a client-side workaround, and it goes stale between reading the crew and creating the plan (plan creation is the offline push path). Either decide that the client filters by `addable` and record that in the plan, or skip non-addable seeds and return them in the response.
3. **Group-scope pull returns 422 for the whole batch.** The plan text says old group scopes behave like any unknown scope (410 or a fresh snapshot). `contracts/sync.py:11` narrows `SCOPE_PATTERN`, so a pull that mixes one stored `group:` scope with valid scopes fails entirely; the test asserts 422 (`test_sync_pull.py:306-315`). No impact while the app has never synced. Align either the plan or the code (return the scope as `unavailable`).
4. **Breaks outside `accepted-breaks.json` (the checker cannot see them).** `check_openapi_compatibility.py` compares only top-level response properties, newly required inputs, and paths. All of the following are intentional, but none is listed:
   - Request properties removed under `additionalProperties: false`, so old clients now get 422:
     - `POST /v1/plans`: `group_id`, `include_all_group_members`, `kind`, `visibility`
     - `PATCH /v1/plans/{id}`: `kind`, `visibility`
     - `POST /v1/plans/{id}/duplicate`: `group_id`, `include_travel_details`
   - Nested response properties removed:
     - `GET /v1/plans` `items[].{group_id, series_id, occurrence_key, is_series_exception, kind, visibility}`
     - `POST /v1/invites/redeem` `plan.{…same}` and the whole `group.*` object
     - `POST /v1/invites/preview` `plan.kind`, `group.name`
   - Sync push commands:
     - **renamed:** `plan.participant.change_role` → `plan.participant.update`
     - **removed:** `plan.join`, `group.*` (10), `series.*` (3), `travel.*` (4)
   - Sync payloads: the `PlanEntity` fields are gone; `AccessLevelName` lost `reader` and `invited`.
   - The `group_id` query parameter on `GET /v1/plans` is now silently ignored.

   Pre-launch this costs nothing, but the success criterion "every break listed" holds only for what the checker can detect. Either extend the checker (request properties, nested properties) or record these classes as a single reviewed note.
5. **`plan_write_guard` does not pin `type`, although the spec says it is "fixed at creation"** (`000007:101-116`). Only the API enforces it. An owner or admin insider can flip a hangout to a trip with direct SQL; the shape checks still hold. Add `NEW.type <> OLD.type` to the immutable list.
6. **Guest-owned crews are lost when the guest claims an existing account.** `create_crew` has no `require_registered`, and `guest_claims.transfer_guest_participations` does not move crews. They stay owned by the now-disabled guest user. Crews that list a guest also keep the stale guest user ID after the claim. Either restrict crews to registered users or transfer `owner_user_id` (and rewrite `member_user_ids`) during the claim.
7. **Capability audit does not record values** (`participants.py:293-295`). The metadata holds `changed: "capabilities"` plus role and state, not the granted set or the before and after values. An expense dispute ("was Bea allowed to void it?") cannot be answered from the audit log. Add `capabilities` and `default_share` (old and new) to the metadata.
8. **Duplicate does not suffix the title** (`duplication.py:69`). The plan says "title (suffixed)"; the code uses `options.title or source.title`. Trivial; decide which is right.
9. **Backfill writes no change rows** (`000007:200-241`). Every existing plan and participant gains `type`, `pass_color`, and `avatar_color` with no new version and no scope generation bump, so a client that had cached these entities would never receive the new fields. This is acceptable only because no client has synced yet; worth one line in the migration header.

---

## Phase 1 acceptance (functional requirements and success criteria)

| # | Item | Verdict |
|---|---|---|
| F1 | type trip/hangout, activity enum, kind backfill (`trip`→trip; outing/custom→other) | Pass. Type is fixed in the API only (Low-5). |
| F2 | destinations 0–10 with shape rules, pass_color with a deterministic default, expected_size 1–50, hangouts reject destinations | Pass. `country_code` is checked by pattern only, not against ISO. |
| F3 | default_share, avatar_color chosen on join and editable by the member, capabilities | Partial: M1 (admin→owner), M2 (grant lifecycle). |
| F4 | MANAGE_EXPENSES/MANAGE_BUDGETS capability-aware, every other rule unchanged | Pass. The policy sweep and the "capability unlocks nothing else" test pass. |
| F5 | users.default_currency | Pass. It also applies to trips (a superset). |
| F6 | Crews: limits, people-only, commands, from-plan, sync, start-with-crew | Pass except start-with-crew (Low-2). Uses `member_user_ids uuid[]` (accepted deviation). |
| F7 | Removals | Pass. Grep is clean; the `pg_catalog`/`pg_policies` test passes. |
| F8 | Duplicate copies the listed fields and not the excluded ones | Pass except the title suffix (Low-8). |
| NF | One forward-only migration with a header; accepted-breaks file with reason and release; checker unit test | Pass, with Low-4 (checker blind spots). |
| SC1 | No references to groups, series, travel, or visibility | Pass |
| SC2 | Trip and hangout create/update/duplicate via REST and push, with replay and conflicts | Pass (`test_sync_push`, `test_idempotency`). |
| SC3 | expenses.manage revises and voids others' expenses; without it they cannot; managers always can | Pass. Void is tested; revise uses the same `_open_expense` gate. |
| SC4 | Crews invisible to non-owners over REST, sync, and direct SQL | Pass (`test_crews`). |
| SC5 | A crew member shares an active plan with the owner at insert time | Pass in the API. The DB guard is deliberately looser (it accepts someone who has left or is pending) and the header documents this. |
| SC6 | `000007` upgrades a database seeded with groups, series, travel, and visibility | **Fail when a series job is queued** (H1). Pass otherwise. |
| SC7 | Full suite on real PG with no skips; coverage ≥ 80%; lint, format, and mypy clean; OpenAPI exported and breaks listed | Pass (452 tests, 0 skipped, 95%), with Low-4. |

## Touchpoint regression check

- **Identity:** no change beyond `default_currency`. Removing `refresh_member_name` only affected groups.
- **Invites:** redeem, rejoin, claim, and email binding are unchanged. The avatar colour flows through. `participant_count` and `destination_names` appear in the preview, which is product intent.
- **Guest claims (including guest → existing account merge):**
  - `guest_claims.py` is unchanged.
  - Guest rows never hold capabilities, and the guard pins `default_share`/`capabilities` on self-updates, so relinking and merging still pass.
- **Finance:** `_open_expense` and `open_ledger` still take the plan row lock first. Capability grants also lock the plan row (`load_plan(for_update=True)`), so a grant or revoke cannot interleave with a finance command. `on_behalf_of` removal: the ledger and the recorder fall back to `None`, which matches the old behaviour for worker callers.
- **Sync:**
  - Directory and scope access lost the group and reader branches correctly.
  - The user scope gains `crew`, loaded under owner RLS.
  - The plan scope is unchanged apart from the entity shape.
  - Push ordering: crew operations fall into the `"user"` bucket, which is conservative and correct.
- **Duplication:** roles are copied (owner becomes admin), and capabilities only for members. The settings update after `add_seeded_participant` stays at version 1. That is fine because the change rows are flushed at commit and the projection reads current state.

## Security notes (verified, no action)

- **Self-grant:** blocked in the API (403) and in the guard, both for update and for self-insert with capabilities or a non-default share.
- **Viewer, guest, and placeholder grants:** the API refuses them with 422.
- **RLS rewrites:**
  - `actor_shares_context`, `actor_can_view_plan`, and `actor_can_view_scope` are correct.
  - `plans_insert` now applies to `api_runtime` only; the worker no longer inserts plans.
  - The rejoin branch of the participant guard now requires an invite.
  - `SECURITY DEFINER` and `search_path` are preserved by `CREATE OR REPLACE`; the crew guard has EXECUTE revoked from PUBLIC.
- **Drop order:** policies and functions are rewritten before the columns and tables are dropped, there is no `CASCADE`, and the empty schema is dropped last. `SET CONSTRAINTS ALL IMMEDIATE` correctly flushes the deferred owner checks before the `ALTER TABLE` statements.
- **Crews:** owner-only SELECT/INSERT/UPDATE, no DELETE grant, no worker grant. The guard pins the ID, owner, and `created_at`, and keeps tombstones final.

## Recommended actions (priority order)

1. H1: set `search_path` for the job-queue DELETE in `000007` (or drop the DELETE); add a migration test with a queued series job.
2. M1: enforce `can_manage_participant` for `default_share`/`capabilities` on other people's rows; add an admin→owner probe to `test_trips_and_members`.
3. M2: clear capabilities on leave, remove, reactivate, and any role change off `member`; consider the DB CHECK; add tests for demote/re-promote and remove/re-add.
4. Low-1, Low-2, Low-5, Low-7: small code fixes. Low-3, Low-4, Low-8, Low-9: decide, then record in the plan or the checker.

## Metrics

- Type coverage: mypy strict, 127 source files, 0 issues.
- Test coverage: 95% (452 passed, 0 skipped, live PG 18).
- Lint issues: 0.

## Unresolved questions

1. Start-with-crew: should the server skip and report non-addable seeds, or is client-side filtering by `addable` the accepted contract? (Low-2)
2. Group scope pulls: should they return 422 or `unavailable`/410 as the plan states? (Low-3)
3. Should crews be limited to registered users? (Low-6)

Status: DONE_WITH_CONCERNS
Summary: Gates are green (452 passed, 0 skipped, 95% coverage; lint, format, mypy, and OpenAPI clean). Migration 000007 fails whenever a series job is still queued (H1). An admin can change the owner's default share (M1). Capability grants outlive demotion, removal, and leaving (M2).
Concerns/Blockers: Fix H1 before 000007 runs in any shared environment whose worker served the `plans` queue.
