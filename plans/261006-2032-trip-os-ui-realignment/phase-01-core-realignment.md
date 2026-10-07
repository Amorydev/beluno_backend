---
phase: 1
title: "Core realignment"
status: completed
priority: P1
effort: "1.5–2 weeks"
dependencies: ["PR #4 fixes (same branch)"]
---

# Phase 1: Core realignment

## Overview

Reshape the top of the domain to match the UI: a plan is a `trip` or a `hangout`; crews replace groups; groups, plan series, travel details, and group visibility go away. Add the plan and member fields the trip and member screens show, and per-trip permission grants. Money behaviour changes are Phase 2.

## Context

- UI screens: Create trip (S35), New hangout (S03), Trip settings (S10), Members (S17, S01 member actions), Invite people (S15), Join trip (S25), Crew detail (S45), People (S29), Home (S61), Me (S49). Text extracts: Stitch project `4246966303110169271`.
- Spec: `~/juntro/docs/trip-os-product-blueprint.md` §8, §9.1–9.2, §9.21–9.22, §22.2.
- Gap report: `plans/reports/ui-backend-gap-analysis-261006-2021-stitch-ui-vs-backend-report.md`.
- Conventions and database traps: `CLAUDE.md` (RLS without `INSERT ... RETURNING`, forward-only numbered migrations, `record_mutation`, `open_context`).

## Requirements

### Functional

1. **Plan type.** `type` is `trip` or `hangout`, fixed at creation. `kind` becomes `activity`, an optional icon key for hangouts only: `dinner`, `drinks`, `karaoke`, `coffee`, `movie`, `sport`, `birthday`, `other`. Existing rows: `kind = 'trip'` → trip; anything else → hangout with the mapped activity.
2. **Trip fields.**
   - `destinations`: an ordered list, 0–10 entries of `{name 1–80, code ≤ 5 uppercase letters, country_code ISO 3166-1 alpha-2, start_date, end_date}`. Only `name` is required. Replaced as a whole on update.
   - `pass_color`: one of `indigo`, `plum`, `sea`, `forest`, `rust`, `slate`, `wine`, `moss`. When omitted, the server picks one deterministically from the plan ID.
   - `expected_size`: optional 1–50, for "6 of 8 slots".
   - Hangouts reject `destinations`.
3. **Member fields.**
   - `default_share`: integer hundredths, 1–10000, default 100 (1.0×). Shown as "Default share" and used by clients as the default shares-split weight.
   - `avatar_color`: one of the member palette keys; chosen on join and editable by the member.
   - `capabilities`: a set drawn from `expenses.manage` ("Edit others' expenses") and `budgets.manage` ("Manage budget"). Owners and admins hold both implicitly. Managers grant capabilities to registered active members.
4. **Policy.** `MANAGE_EXPENSES` allows owner/admin or the `expenses.manage` capability; `MANAGE_BUDGETS` allows owner/admin or `budgets.manage`. Every other rule stays.
5. **User default currency.** `users.default_currency` is set from Me → Preferences and is the default currency of a new hangout.
6. **Crews.**
   - A crew is a private, saved list of people owned by one user: name 1–60, members 1–50.
   - Members must be users the owner currently shares an active plan with ("people").
   - Commands: create (optionally from a plan's active members, "Saved from Friday hotpot"), rename, replace members, delete (tombstone).
   - Sync entity `crew` in the owner's user scope.
   - "Start a trip/hangout with this crew" reuses plan creation with `participants` seeds. Registered members are added directly; guests are skipped and reported so the client shows the invite link.
7. **Removals.**
   - Groups: tables, group invites, group-visible plans, the `reader` access level, the group sync scope, group access signals, `PlanAction.JOIN`, `GroupAction`, and `include_all_group_members`.
   - Plan series and recurrence, plus the `plans.extend_series_horizons` job.
   - Travel details and segments.
   - Plan `visibility` and `group_id`.
8. **Duplicate.** "Clone members and settings" copies type, title (suffixed), base currency, destinations, pass color, expected size, and registered active members with their role, default share, and capabilities. It does not copy dates (the client sends new ones), finance, invites, guests, or placeholders.

### Non-functional

- One forward-only migration `000007_trip_first_realignment` with the standard header (forward action, lock/scan risk, validation queries, compatibility, rollback).
- No behaviour regression in identity, invites, guest claims, finance, or sync for plans.
- The OpenAPI compatibility gate gains an explicit, reviewed accepted-breaks list (`openapi/accepted-breaks.json`) so this phase's intentional removals pass CI. Each entry has a reason and the release it belongs to; entries are pruned once `main` carries the new contract.

## Architecture

### Data model after the phase

```text
iam.users (+ default_currency)
plans.plans (type, activity, destinations jsonb, pass_color, expected_size;
             − group_id, series_id, occurrence_key, is_series_exception, visibility)
plans.plan_participants (+ default_share, avatar_color, capabilities text[])
plans.plan_invites (unchanged)
people.crews (id, owner_user_id, name, source_plan_id, version, timestamps, deleted_at)
people.crew_members (crew_id, user_id, position)
finance.* (unchanged)
```

- `destinations` is JSONB because nothing queries inside it. Pydantic validates the shape; the database checks that it is an array of at most 10 items.
- RLS:
  - `people.crews` and `people.crew_members` are owner-only (`owner_user_id = iam.actor_id()` through the crew).
  - `crew_members` inserts also check `iam.actor_shares_context(user_id)`.
  - `iam.actor_shares_context` and `plans.actor_can_view_plan` lose their group branches.
- Write guards: extend the `000003` participant guard so non-managers can change only their own `avatar_color` and only managers change `default_share` and `capabilities`. Crews need no insider guard beyond owner-only RLS.

### Sync after the phase

| Scope | Entities |
|---|---|
| `user:{id}` | user, session, crew, plan_access |
| `plan:{id}` | plan, plan_participant, plan_invite, finance entities |

Access levels: `self`, `manager`, `member`. `reader`, `invited` (group), and the group scope are removed. The client protocol version stays 1. Directory responses no longer list group scopes, and old cursors for group scopes return `410`/fresh-snapshot as for any unknown scope (verify this in tests).

### Migration order (`000007`)

1. Add the new columns with backfills (type and activity from kind, `pass_color` from the ID hash, `default_share = 100`, member avatar colours by position, empty capabilities), then `SET NOT NULL` where required. Tables are small before launch.
2. `CREATE OR REPLACE` every function and policy that references `groups.*`, `plan_series`, travel, or `visibility`. List the dependents first with `pg_depend` and record the query in the header.
3. Drop the travel and series tables, the plan columns, and the `groups` schema objects explicitly. No `DROP SCHEMA ... CASCADE`.
4. Create the `people` schema, crews tables, RLS, grants, and functions (`REVOKE EXECUTE ... FROM PUBLIC`).
5. Drop `'group'` from the `scope_type` checks in `sync_audit` and replace `sync_audit.actor_can_view_scope`. Delete group-scope change rows and heads first.

## Related Code Files

- Create:
  - `alembic/versions/000007_trip_first_realignment.py`
  - `src/beluno/db/models/people.py`
  - `src/beluno/modules/people/crews.py`
  - `src/beluno/contracts/people.py`
  - `src/beluno/api/routers/crews.py`
  - `src/beluno/api/commands/crews.py`
  - `openapi/accepted-breaks.json`
  - `docs/adr/0008-trip-first-realignment.md`
- Modify:
  - Plans: `src/beluno/db/models/plans.py`, `src/beluno/contracts/plans.py`, `src/beluno/modules/plans/{service,participants,duplication,changes,timing,invites}.py`
  - Policy and access: `src/beluno/authorization/{policy,access}.py`
  - API: `src/beluno/api/{main,presenters,projection}.py`, `src/beluno/api/commands/{__init__,plans}.py`, `src/beluno/api/routers/{plans,invites,me}.py`
  - Sync: `src/beluno/sync/{scopes,directory,push}.py`, `src/beluno/contracts/sync.py`, `src/beluno/modules/sync_audit/recorder.py`
  - Identity: `src/beluno/modules/iam/users.py`, `src/beluno/contracts/iam.py`
  - Invitations: `src/beluno/modules/invitations.py`
  - Worker: `src/beluno/worker/tasks.py`
  - Testkit: `src/beluno/testkit/{finance,database}.py`
  - `scripts/check_openapi_compatibility.py`, `openapi/openapi.json`
  - Docs: `docs/adr/0002-plan-participant-identity.md`, `docs/adr/0004-offline-sync.md`, `docs/architecture/{data-model,domain-map}.md`, `docs/contracts/{permission-matrix,sync-protocol,error-catalog}.md`, `README.md`
- Delete:
  - `src/beluno/db/models/groups.py`, `src/beluno/modules/groups/`
  - `src/beluno/api/routers/{groups,plan_series,travel}.py`, `src/beluno/api/commands/{groups,series,travel}.py`
  - `src/beluno/modules/plans/{series,recurrence,travel}.py`
  - `tests/integration/test_group_lifecycle.py`, the series half of `tests/integration/test_series_and_duplication.py`
  - `tests/e2e/test_group_plan_lifecycle.py` (rewritten as `test_trip_lifecycle.py`)
- Tests to update (they create groups or use series/travel/visibility):
  - Integration: `test_access_edge_cases`, `test_change_log`, `test_finance_expenses`, `test_finance_fund`, `test_guest_claim`, `test_idempotency`, `test_invite_redemption`, `test_plan_lifecycle`, `test_sync_offline_window`, `test_sync_pull`, `test_sync_signals`, `test_worker_jobs`
  - Security: `test_insider_write_guards`, `test_rls_isolation`
  - Sync: `test_multi_device_model`

## Implementation Steps

1. Write ADR 0008 (trip-first realignment: what goes, what stays, why) and update ADRs 0002/0004 references.
2. Add `openapi/accepted-breaks.json` support to `scripts/check_openapi_compatibility.py`, with a unit test covering an accepted and an unaccepted break.
3. Migration `000007` in the order above; validation queries for each removed and added object.
4. Models and contracts: plan `type`, `activity`, `destinations`, `pass_color`, `expected_size`; participant `default_share`, `avatar_color`, `capabilities`; user `default_currency`. Remove group, series, travel, and visibility fields.
5. Policy: drop groups, `JOIN`, and reader rules; add capability checks to `MANAGE_EXPENSES` and `MANAGE_BUDGETS`; add a command to grant or revoke capabilities and change default share (managers), and to change one's own avatar colour.
6. Plans service: creation with type rules (hangout: no destinations), update rules, duplication rewrite, participant seeds without group expansion.
7. Crews module, commands, routes, user-scope sync entity, and "create from plan".
8. Sync: remove the group scope and reader level from scopes, directory, projection, and push; add `crew` to the user scope.
9. Remove the series job from the worker; delete the removed modules and routes; update testkit fixtures to create plans directly.
10. Update and delete tests as listed; add new ones:
    - type and activity rules
    - destinations validation
    - capability grants, including an IDOR check that one participant cannot grant themselves
    - crews: owner-only RLS, "people only" membership check, sync
    - migration backfill on a seeded pre-`000007` database
11. Export OpenAPI, record accepted breaks, update docs, and run the full gate set.

## Success Criteria

- [x] No code, table, function, policy, route, or sync type references groups, series, travel, or visibility (grep and `pg_catalog` checks in a test).
- [x] Trip and hangout create/update/duplicate work through REST and sync push, with replay and conflicts as before.
- [x] A member granted `expenses.manage` can revise and void others' expenses; without it they cannot; owners and admins always can.
- [x] Crews are invisible to everyone but their owner in REST, sync, and direct SQL as `api_runtime`.
- [x] A crew member must share an active plan with the owner at insert time.
- [x] Migration `000007` upgrades a database seeded with groups, series, travel, and group-visible plans without errors, and the validation queries pass.
- [x] Full suite passes on real PostgreSQL with none skipped; coverage ≥ 80 %; ruff, format, mypy strict clean; OpenAPI exported with every break listed in `accepted-breaks.json`.

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Hidden dependents of group objects block drops or silently change policies | Enumerate them with `pg_depend` before writing the migration; explicit drops only; RLS isolation suite must stay green. |
| Sync cursors for removed group scopes confuse clients | The app has never synced; still test that unknown scopes return the documented re-snapshot error. |
| Capability grants open an escalation path | Only managers grant; the write guard enforces it; the security suite includes self-grant and admin-demotion cases. |
| Large blast radius (27 source files, 18 test files) | One command group at a time; full suite after each step; no unrelated refactors. |

## Rollback

Forward-only. Before release 1 nothing depends on the removed objects. If the migration fails in a shared environment, restore from the pre-migration backup (pre-launch data only). Never re-create groups by reversing the migration.

## Completion Notes (2026-10-06)

Built on the PR #4 branch (single branch, user decision). Commits: removals, trips/hangouts with member settings and capabilities, crews, catalog check, docs, review fixes. Gates: 455 passed, 0 skipped, coverage 95 %, ruff/format/mypy clean, OpenAPI exported, 76 accepted breaks.

Deviations from this file, decided during implementation:

- Crews store `member_user_ids uuid[]` on `people.crews`; no `crew_members` table (nothing queries members on their own). A write guard backs the API membership check.
- "Start with this crew": the crew response marks each person `addable` (registered, active, sharing an active plan). The client seeds only addable people and shows the invite link for the rest; a guest seed still fails plan creation with `409 GUEST_NOT_ALLOWED` instead of being skipped server-side.
- A `group:` scope is now an invalid scope type: pull and handshake return `422` for the batch, not a per-scope re-snapshot. No client ever synced one.
- Duplicate takes the new title from the client; the server adds no suffix (an English-only string would leak into Vietnamese UI).
- The migration backfills new plan and participant fields without change rows; acceptable only because no client has synced.

Review fixes (code-reviewer report in `reports/`):

- Migration deleted queued series jobs without the job schema on the search path; Procrastinate's delete trigger failed and rolled back the upgrade. Fixed and covered by a seeded-queue migration test.
- Admins could change the owner's or another admin's default share, capabilities, or colour. Now any change to someone else's row needs `can_manage_participant`.
- Capabilities survived demotion, leaving, removal, and ownership transfer. Now cleared on each, with a check constraint (`capabilities = '{}'` unless an active member with an account).
- Also: explicit nulls rejected on participant update; plan `type` immutable in the write guard; ADR 0008 lists breaks the OpenAPI checker cannot see.

Follow-up (user decision 2026-10-06): crews are for registered accounts only. A guest cannot own a crew or be listed in one; "save from plan" keeps the registered people. The API and `people.crew_write_guard` both enforce it, so a guest account retired by a claim never strands a crew.
