# Code review: planning slice 2 (polls), uncommitted tree on `feat/planning-polls`

Date: 2026-10-07. Reviewer: code-reviewer. Read-only; probes ran in scratch DBs (dropped by fixtures).

## Scope
- Migration `alembic/versions/000012_decisions_polls.py`, models `src/beluno/db/models/decisions.py`,
  service `src/beluno/modules/planning/polls.py`, `places.py` (status kwarg, `mark_poll_winner`),
  contracts, commands, router, projections, worker task, activity events, testkit, tests, docs.
- Gates: `ruff check` clean, `ruff format --check` clean, `mypy src` clean (150 files).
  `tests/integration/test_planning_polls.py tests/security tests/integration/test_plan_purge.py`: 233 passed (live PG, not skipped).
- OpenAPI snapshot matches a fresh export; compatibility check vs `HEAD` passes; diff is additive
  (5 paths, 8 new schemas, no existing schema changed). The 6.8k-line git diff is alignment noise.

## Overall
The core close path is good: one DEFINER path, a poll-row lock, an idempotent re-entry,
`SKIP LOCKED` for the job, and a vote path that locks the poll first. A live probe interleaving
vote and close (6 runs, both orders seen) always gave `poll_votes` count = `result.voted`.
The weak spots are (a) the DB guards: they do not hold the invariants the migration header
claims against an insider, and (b) the outcome actions: tie picks and free-text winners can
diverge across the two actions. No Critical findings.

## Critical
None.

## High
None.

## Medium

**M1. `guard_poll` lets any active participant, viewers included, rewrite an open poll and close it through the deadline**
`000012_decisions_polls.py:152-171`. On an open poll the guard pins only identity columns,
status, and `version + 1`. Probed as viewer Dan via `api_runtime`, this UPDATE was ALLOWED:
`UPDATE decisions.polls SET question='Hijacked', deadline_at = now() - interval '1 min', allow_vote_change=false, version = version + 1`.
- Failure: an insider rewrites the question after votes, so existing ballots now mean something else. Or the insider moves `deadline_at` into the past, and `close_due_poll` closes the poll within 5 minutes with `closed_by = NULL`. That gets around `close_poll`'s "creator or organiser" check, which is the invariant the header states ("a guarded runtime never closes a poll itself"). Viewers can also tombstone anyone's poll (`deleted_at`, ALLOWED). That last part matches the slice-1 `guard_root` convention, but the close bypass is specific to polls.
- Fix: the service never edits a poll, so on an open poll allow only `version`, `updated_at`, and `deleted_at`:
  `to_jsonb(NEW) - 'version' - 'updated_at' - 'deleted_at' IS DISTINCT FROM to_jsonb(OLD) - ... → raise`
  (and keep the existing closed-poll rule). Add the deadline-rewrite attempt to `test_the_database_keeps_votes_honest`.

**M2. No INSERT guards on `polls` and `poll_outcomes`: an insider can insert pre-closed polls and fake outcomes**
`000012_decisions_polls.py:222-229`, `235`. The guards are BEFORE UPDATE on polls and BEFORE INSERT on options and electorate only. Probed as viewer, both inserts were ALLOWED:
- An INSERT into `decisions.polls` with `status='closed'`, `closed_at=now()`, `version=7`, and `created_by_user_id` = the owner. The result is a closed poll with no `poll_results` row, attributed to someone else.
- An INSERT into `decisions.poll_outcomes` on an *open* poll, with an arbitrary option, an arbitrary `created_entity_id`, and `created_by_user_id` = the owner. Because the PK is `(poll_id, action)`, the real `apply_outcome` later either returns the fake entity as "already applied" (same option) or answers 409 `OUTCOME_ALREADY_APPLIED` (other option). The legitimate action is blocked for good.
- Fix: on `polls`, add a BEFORE INSERT guard requiring `status='open'`, `version=1`, `closed_* IS NULL`, `deleted_at IS NULL`, and `created_by_user_id = iam.actor_id()`. On `poll_outcomes`, add a BEFORE INSERT guard requiring the poll to be closed with a result, `option_id` = `winner_option_id` or in `tied_option_ids`, and `created_by_user_id = iam.actor_id()` (or a merged guest of the actor). Alternatively, write outcomes only through a DEFINER function.

**M3. "Written when the poll opens" is really "written any time before the first vote"; the electorate is computed in client code**
`000012_decisions_polls.py:174-190`; `polls.py:151-162`. `create_poll` leaves the poll at version 1. Until someone votes, which can take days, an insider (viewer included) can append options and electorate rows. Probed: an extra option at position 5 was ALLOWED, and placeholder Cam was added to the electorate (ALLOWED). Neither bumps the version or writes a change row, so devices never learn of it. The repo test only checks the post-vote case, and its comment acknowledges the gap. The creating transaction can also put any set of same-plan participants in the electorate: the "active, non-placeholder" snapshot lives only in Python.
- Fix (preferred): add a DEFINER `decisions.open_poll(...)` (or one that only fills options and electorate). It computes the electorate in SQL from `plan_participants` (active, not placeholder), and the guard then refuses direct inserts. Lighter fix: in the guard, require that the poll row was inserted by the current top-level transaction. `polls.xmin` is a subtransaction XID because of the savepoint in `create_poll`, so compare using `pg_xact_status`, or drop the savepoint.

**M4. Tie-break is per action, so `save_place` and `add_to_plan` can pick different tied options**
`polls.py:245-253`. The idempotency lookup is `(poll.id, request.action)` only. On a `tie`, an organiser can run `save_place` with option A and then `add_to_plan` with option B. Both succeed, and the poll's `outcomes` list shows two different "winners". The user decision says an organiser *picks one* of the tied options.
- Fix: before applying, load any existing outcome for the poll. If its `option_id` differs from the chosen option, return 409 `OUTCOME_ALREADY_APPLIED`. Add a test.

**M5. A free-text winner gets an unlinked place and item when both actions run**
`polls.py:258-273`, `381-386`. Probed: `save_place` on a free-text option creates place P (`poll_winner`). A later `add_to_plan` creates an item with `place_id = None` (confirmed: item `place_id None`, saved place `01a1…`). The reverse order behaves the same way. The trip then holds a duplicate place and item for one decision, and the place never shows as used by the item.
- Fix: in `add_to_plan`, take `place_id` from an existing `save_place` outcome when `option.place_id` is None. In `save_place`, when an `add_to_plan` outcome exists, link that item to the new place, or document that the two actions are independent.

**M6. Yes/no quorum semantics need confirmation (product logic)**
`000012_decisions_polls.py:303-312`; contract `contracts/planning.py:205`. With a quorum, a poll passes once yes ≥ quorum, however many vote no: quorum 2 with 2 yes and 10 no passes. Most users read "quorum" as minimum turnout plus a majority. Two more cases: zero votes give `failed`, not `no_votes`. A quorum above the electorate is accepted (probe: quorum 50 with 4 eligible), so the poll can never pass. The docs describe the current behaviour, but the plan's user decisions do not cover it.
- Fix after a decision: either rename the field to `yes_needed` and refuse values above `eligible` at creation, or implement "turnout ≥ quorum AND yes > no".

## Low

- **L1. The Python/DB deadline boundary returns 500.** `polls.py:389-391` checks `ctx.now` (Python clock at request start). `guard_vote` checks `transaction_timestamp()`. A vote that lands in the gap gets `insufficient_privilege`, which nothing maps (grep finds no handler), so the client sees a 500 instead of 409 `POLL_CLOSED`. The window is tiny but real at the deadline. Fix: read the time from the DB for this check, or map 42501 from poll guards to 409.
- **L2. A poll past its deadline still shows `status: "open"` for up to 5 minutes plus queue lag.** Votes get 409 in that window, `delete_poll` (`polls.py:224`) still lets the creator withdraw it, and an organiser close then records the organiser as the closer. Document this for clients or add an `accepting_votes` flag. Consider refusing delete once the deadline has passed.
- **L3. `close_poll` in SQL is looser than the API.** `000012:378-387` lets the creator close regardless of current role (viewer) and plan state (archived or deletion scheduled). Python (`require_author_or_manager`) is stricter, so this matters only for insiders. Keeping the two aligned avoids surprises.
- **L4. Merging into an existing participant row keeps both votes.** When a guest and the account were both in the electorate and both voted, the merge keeps two ballots and `eligible` counts both. Rare; decide whether `finalize_poll` should count only non-merged electors.
- **L5. Audit action naming is inconsistent.** The DEFINER writes `decisions.poll_closed`; Python writes `planning.poll_*`. Pick one prefix if audit queries filter by prefix.
- **L6. Possible deadlock between the purge and the close job (informational).** `plans.purge_deleted_plan` locks the plan, then deletes polls. `finalize_poll` holds the poll lock, then inserts `activity.events` (the FK takes KEY SHARE on the plan). If both hit the same plan at once, one deadlocks and Procrastinate retries (`retry=3`). This needs a deadline elapsing on a plan that is 7+ days into deletion, at the moment the purge runs.
- **L7. Test gaps.** Every case below was verified by probe, but none has a repo test:
  - viewer votes (200) and guest votes and creates (200 / 201); a viewer cannot create (403);
  - a deadline in the past is refused (422);
  - delete of an open poll (204 then 404) and of a closed poll (409);
  - hangout (409 `NOT_AVAILABLE_FOR_HANGOUT`);
  - `no_votes` outcome (409) and outcome on a yes/no poll (409 `POLL_NOT_DECIDED`);
  - free-text `save_place` (the `create_place(status=poll_winner)` path).
  - `test_the_deadline_closes_a_poll_once_even_racing_an_organiser` runs sequentially and proves no race. Add a lock-held interleaving like the scratch probe (hold the poll row, then fire vote and close as tasks).
  - No test covers the M1 to M4 cases.

## Verified clean
- **Close race.** `vote` takes `SELECT … FOR UPDATE` on the poll before the guard runs. `finalize_poll` locks the row, re-reads it after the wait, returns early when the poll is closed, and returns NULL when it is deleted. `close_due_poll` uses `SKIP LOCKED`. The live interleaving probe was consistent in both orders. The job loop commits each poll in its own transaction and stops on NULL.
- **Tally.** Ties come out correctly (leaders = options at `top`, `top > 0` when `voted > 0`, ordered by position). `winner` clears `tied_option_ids`. Counts are keyed by option UUID, so the summary carries no text (`{outcome, option_id}`, keys added to `SUMMARY_KEYS`).
- **Versions and sync.**
  - Create gives v1, each vote +1, the DEFINER close +1 (its change row uses `poll.version + 1`, matching its UPDATE), each outcome +1.
  - `_find(refresh=True)` repopulates the poll after the DEFINER close.
  - A closed-poll outcome bump passes `guard_poll` (only `version` and `updated_at` change).
  - The poll entity carries options, voter_ids, eligible, result, and outcomes; tombstones come back as `None` from `load_poll`.
  - The test confirms pull sees the job-closed poll and the feed event with a null actor.
- **Grants.** `finalize_poll` has no grants. `close_poll` goes to `api_runtime` only and `close_due_poll` to `worker_runtime` only. `poll_results` is SELECT-only. No DELETE grants anywhere. All 6 tables have RLS, and the catalog-driven RLS sweep picks them up automatically.
- **Event and audit IDs from the caller.** Same pattern as `activity.append_events`. Misuse yields a PK error or a mis-sorted feed entry; the only actors who can trigger it are organisers or the worker, who already may close.
- **Electorate.** Active and not placeholder; guests and viewers are included and can vote (probed). Late joiners get 403. Votes are pinned to the voter by `guard_vote` (actor's own participant row) and the electorate FK.
- **Purge.** Decisions rows are deleted children-first, before places and participants. Every new `plan_id` table has an index for the purge deletes. The purge test and IDOR sweep cover the `decisions` schema through the catalog. Users and participants are never hard-deleted elsewhere, so the new FKs to `iam.users` and `plan_participants` block nothing.
- **Permissions.** Python `require_author_or_manager` and SQL `close_poll` agree on owner/admin and creator (merged guests included). Closed polls cannot be deleted (409).
- **N+1.** `poll_views` runs a constant 5 queries per batch; every lookup hits a PK or a leading-column index.

## Recommended actions (priority)
1. M1: pin all open-poll columns except `version`, `updated_at`, and `deleted_at` in `guard_poll`.
2. M2: add INSERT guards on `polls` and `poll_outcomes`, or move outcomes into a DEFINER function.
3. M4: one tie pick per result across actions.
4. M3: compute the electorate in a DEFINER `open_poll`, or close the version-1 window.
5. M5: link a free-text winner's place and item across the two actions.
6. M6: get the user's call on quorum semantics, and validate quorum against `eligible`.
7. L1 and L7: map the boundary 500 and add the missing tests, including a real interleaving race test.

## Plan follow-ups
- Slice 2 design items are implemented: schema, close path, outcomes, permissions, sync and feed, purge, docs, OpenAPI.
- The checklist item "Poll close races (job vs organiser) yield one result version" holds by code and probe, but the repo test is sequential (L7).

## Unresolved questions
1. Quorum semantics: a yes-vote threshold regardless of no votes (current), or turnout plus majority? Should a yes/no poll with zero votes close as `no_votes`?
2. Should a tie pick bind both outcome actions (M4)? The design text suggests one pick.
3. Is the insider (raw `api_runtime` SQL with an actor) threat model expected to cover authorization columns on planning tables? Slice 1 `guard_root` does not; M1 and M2 matter more here because they bypass the close and result invariants.

Status: DONE_WITH_CONCERNS
Summary: The close path is race-safe and correct (live interleaving probe consistent), and all gates and target suites pass (233 tests). The DB guards leave open-poll columns, poll and outcome inserts, and the version-1 window writable by any active participant, which allows a close bypass via the deadline and blocks outcomes. Tie and free-text outcome actions can diverge.
Concerns/Blockers: M1 to M5 should be fixed before merge; M6 needs a user decision on quorum semantics.
