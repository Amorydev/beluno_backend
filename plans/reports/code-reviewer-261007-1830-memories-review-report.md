# Code Review: memories (feat/memories, uncommitted)

## Scope
- Files: alembic/versions/000018_memories.py; src/beluno/modules/{media,recap,account_deletion}.py; src/beluno/api/{commands,routers}/media.py, routers/recap.py, media_projection.py; contracts/{media,recap}.py; db/models/media.py; testkit/{media,tenants}.py; tests (test_memories new, test_media, test_recap, test_plan_purge, idor sweep); docs, openapi.
- Gates run: ruff check/format OK; mypy strict OK; openapi export in sync (cmp), compat check vs main rc=0; `test_memories/test_media/test_recap/test_plan_purge/test_idor_sweep` 19 passed on Testcontainers PG (not skipped).
- Probes (scratch pytest, live PG + moto + fake clamd): concurrent highlight picks at limit -> [200, 409]; insider uploader delete smuggling `in_recap=true, caption='x'` -> accepted; tombstoned place kept on edit -> 200; completed trip -> create 403, highlight (pick or unpick) 403, edit 403, delete 204, recap 200.

## Overall
Guard and service agree on every API path checked (details edit, highlights via `actor_manages_media(plan, NULL)`, scanning transition freezes details, worker settle freezes details, receipt deletion; DB capability CHECK limits `expenses.manage` to members so the DB is never stricter than `decide_plan`, which means no 500s). The highlight limit holds under concurrency (plan `FOR UPDATE`). `forget_memories` is correctly scoped (memory kind only, merged guests included, plan-scope delete rows carrying post-update version, deletion trigger queues objects, covers untouched, `in_recap` cleared) and runs before sessions/profile scrub while `app.actor_id` is still set. The real gaps are product flow after completion, merged-guest authorship parity, and migration header accuracy.

## Critical
None.

## High
**H1. Memories and highlights freeze once the trip is completed, which is when the recap is used.** `media.py` `_plan` (MEMORY -> `RESPOND_PLANNING`), `update_memory` (`RESPOND_PLANNING`), `set_highlight` (`UPDATE`) all use `EDITABLE_PLAN_STATES` (draft..settling). Probe: after `active -> completed`, adding a memory, editing a caption, and picking *or dropping* a highlight all 403, while delete still 204 (VIEW). Organisers cannot curate highlights on the recap screen of a finished trip, and people cannot add photos after the trip ends unless the owner reopens it.
Fix: confirm with the user first, then add dedicated actions, e.g. `SHARE_MEMORIES = Rule(ALL_PLAN_ROLES, EDITABLE_PLAN_STATES | {COMPLETED})` and `PICK_HIGHLIGHTS = Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES | {COMPLETED})`. The DB guard does not check plan state, so no migration is needed. Add a completed-state test.

## Medium
**M1. Merged-guest authorship ignored for memory edit/delete and receipt-creator deletion, though `forget_memories` follows merges.** The codebase pattern is `users.is_actor_account` + `iam.user_merged_into_actor` (used by `finance/expenses.py:529`, `settlements.py:303`, `planning/common.py:51`). Here: `media.py update_memory` (`uploaded_by_user_id != actor`), `_may_delete` (`uploaded_by_user_id == actor`, `creator == actor`); guard `actor_manages_media` (`p_uploader = iam.actor_id()`) and `actor_may_delete_media` (`e.created_by_user_id = iam.actor_id()`). Scenario: a guest shares photos, later merges into their account. They can no longer edit or delete those photos or delete receipts on expenses they created as a guest (while expense edit/void works), yet account deletion erases them as theirs.
Fix: service uses `is_actor_account` for uploader and creator checks. A new migration adds `OR iam.user_merged_into_actor(p_uploader)` / `(e.created_by_user_id)` to the two definer functions. Test with a merged guest.

**M2. Migration header claims are inaccurate (000018:19-28).** "Compatibility: additive" is false: `in_recap` is NOT NULL with its default dropped, so the previous build's ORM INSERT (no `in_recap`) fails with a NOT NULL violation. `create_media` catches every `IntegrityError` as 409 `ALREADY_EXISTS`, so every receipt/cover create fails with a misleading error until the new API runs. 000007/000008 state "API and worker must run this revision together". Lock text also omits that the column CHECK, three table CHECKs, and FK validation scan media under ACCESS EXCLUSIVE and hold SHARE ROW EXCLUSIVE on `schedule_places.places` for the migration transaction. The table is small today. Fix: correct the header text.

**M3. `forget_memories` has no index to use (000018:182-189).** It filters on `uploaded_by_user_id` (plus a merged-guest IN). The only indexes are `(plan_id, id)`, expense, and the new `in_recap` partial, so it seq-scans the whole media table, which is the fastest-growing table (photos), inside the account-deletion request transaction. Fix: `CREATE INDEX media_memory_uploader_idx ON media_memories.media (uploaded_by_user_id) WHERE kind = 'memory' AND deleted_at IS NULL;` in the same or a follow-up migration.

**M4. Test gaps (tests/integration/test_memories.py).**
- The merged-guest branch of `forget_memories` is never exercised.
- No check that a deleted account's cover stays (and the plan cover with it).
- DB-guard insider tests cover only two cases (line 301). Missing: INSERT with `in_recap=true`; uploader changing details in the awaiting->scanning UPDATE; worker changing details; non-creator/non-manager receipt delete; organiser highlight success.
- No plan-state tests (H1).
- No viewer-with-capability case. (The DB CHECK prevents it, but nothing asserts 403.)
- The `memory()` helper (line 64-70) names `ready` but returns the pre-scan record.

## Low
**L1. Guard delete branch does not freeze details (000018:127-135).** Probe: as Bea (uploader), `UPDATE ... SET deleted_at=now(), in_recap=true, caption='x'` succeeded. Impact is nil today: deleted rows are hidden everywhere, there is no undelete, and the highlight count filters `deleted_at`. It still lets an insider bypass the "only organisers pick" invariant on the stored row. Fix: on delete, require details unchanged and `NOT NEW.in_recap OR OLD.in_recap`.

**L2. A memory can point at a tombstoned place.** `update_memory` only re-validates when `place_id` changes. Probe: place deleted -> 204, edit keeping that place -> 200. The FK cannot see `deleted_at`. Clients get `place_id` for a place missing from sync. Either clear `place_id` on place delete (with media change rows) or document that clients treat an unknown place as none.

**L3. The voided-expense refusal races `void_expense`.** `_require_expense` reads `Expense.state` unlocked. The INSERT's FK takes KEY SHARE, which does not conflict with void's non-key UPDATE, so a receipt can land on an expense voided concurrently. The DB does not enforce the rule. Accept as best-effort, or check `state = 'active'` in the guard INSERT branch via a definer that reads the expense `FOR SHARE`.

**L4. `create_media` maps every `IntegrityError` to `ALREADY_EXISTS`.** That now also covers the place FK and the new CHECKs. Narrow it to the PK/unique violation (`error.orig.diag.constraint_name`).

**L5. Stale docstring and naming.** `media.py:1` "(and memories later)" is stale. `contracts.media.MemoryDetails` and `modules.media.MemoryDetails` share a name.

## Verified non-issues
- Highlight parity: `require_plan(UPDATE)` (owner/admin) matches `actor_manages_media(plan, NULL)`, since `NULL = actor` yields NULL and the row is filtered.
- Receipt deletion parity: `plan_participants_capabilities_for_members` CHECK means `'expenses.manage' = ANY(capabilities)` implies a member. The DB ignores plan state and deletion scheduling while the service does not, so the DB is only ever looser and never 500s.
- Worker settle re-reads under `FOR UPDATE`, so concurrent caption edits during scanning do not break the version+1 guard.
- forget runs as a definer owner (guard and RLS bypassed by design). Change rows go to plan scope with `entity_version` = new version. The trigger queues `incoming/` and `media/` keys (test asserts). Only `kind = 'memory'` is touched.
- Recap: highlights filter ready, not deleted, `in_recap`, ordered by day then time (nulls last) then id. The count covers ready, undeleted memories. The share card carries only the cover id.
- Captions: `LongText` strips and rejects control chars (probed NUL and blank). `max_length=280` is enforced and matches the DB `btrim` CHECK.
- Exports: media rows come through the sync projection, and CSV formula escaping applies to free text.

## Recommended actions
1. H1: ask the user about post-completion memories and highlights, then add the actions.
2. M1: merged-guest parity (service + definer functions + test).
3. M2/M3: header fix and uploader partial index.
4. M4: add the missing guard/forget/state tests.
5. L1-L5 as cheap follow-ups.

## Unresolved questions
- Should memories be addable/editable and highlights pickable on completed (and archived) trips?
- Account deletion keeps covers the person uploaded. Is that intended (covers are trip-owned like receipts)?
- When a place is deleted, should memories drop their `place_id`?

Status: DONE_WITH_CONCERNS
Summary: Guard/service parity, the highlight limit, and forget_memories scope are correct and the gates pass. Memories and highlights are frozen on completed trips (needs a user decision), merged-guest authorship is not honoured for edit/delete, and the migration header misstates compatibility.
