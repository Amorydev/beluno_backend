## Code Review Summary: nudges, daily summaries, sign-out-others

### Scope
- Files: alembic/versions/000020_nudges_summaries.py, src/beluno/modules/notifications.py, src/beluno/modules/iam/sessions.py, src/beluno/api/routers/{notifications,me}.py, src/beluno/contracts/notifications.py, src/beluno/modules/iam/rate_limits.py, tests/integration/test_nudges_summaries.py, tests/security/test_idor_sweep.py, README, contract doc, plan phase file
- LOC: about 520 added (diff) plus 524 in the new files
- Focus: uncommitted branch `feat/nudges-summaries`
- Gates: ruff and ruff format pass, mypy strict passes, the exported OpenAPI matches the committed file, and the targeted suites pass (13 tests; Testcontainers, so not skipped)
- Live probes (scratch pytest, Testcontainers PG16-alpine, plus plain `docker run` of postgres:16 Debian): results below

### Overall Assessment
The SQL changes match 000019 on the points checked: the trigger keeps every 000019 event type and adds `waiver.given`. The `fan_out` change only adds the waiver branch. `queue_reminders` changes only the time zone handling, and the poll branch is byte-identical. Grants survive `CREATE OR REPLACE`. The loc-arg mapping is correct for every kind.

There are two blocking classes of defect:
1. A profile time zone that the API accepts but PostgreSQL does not know makes the whole dispatch transaction throw every minute. That stops all notifications for every user.
2. `queue_nudge` raises `insufficient_privilege` for recipients the API lets through. The result is an unhandled 500 in common flows: a placeholder who owes money, a self-assigned task, a merged or departed person.

### Critical Issues

**C1. One user's time zone can stop every notification for every user**
- Where: alembic/versions/000020_nudges_summaries.py:138,147-148 (queue_reminders), 252,255,263,277 (queue_summaries); src/beluno/modules/notifications.py:267-269.
- Cause: the API validates `timezone` against Python `zoneinfo.available_timezones()` (src/beluno/contracts/common.py:39-48, tzdata 2026.5). PostgreSQL resolves `AT TIME ZONE` against its own tzdata.
- Verified on `postgres:16` (Debian 13, tzdata 2026b without tzdata-legacy): 113 names that Python accepts are rejected there. They include **`Asia/Saigon`** (the Vietnamese legacy alias), `Asia/Calcutta`, all `US/*`, `Europe/Kiev`, `GMT0`, `UCT`, `Singapore` and `Japan`. Example: `SELECT now() AT TIME ZONE 'Asia/Saigon'` gives `ERROR: time zone "Asia/Saigon" not recognized`. The Alpine image happens to ship all of them; staging uses Alpine, but production PostgreSQL may not.
- Failure path: `dispatch()` runs `fan_out`, `queue_reminders` and `queue_summaries` in one transaction. Probe: setting one user's timezone to an unknown name makes `dispatch` raise `DataError: time zone "Bogus/Zone" not recognized`. It then rolls back `fan_out` and never reaches `deliver()`. Result: every user loses every notification (money, reminders, security) every minute until someone repairs the row by hand.
- For `queue_summaries` the problem appears as soon as any affected user is on an active trip. For `queue_reminders`, it appears when such a user is assigned a task with a due date, since the time-zone expression sits in the join filter.
- Fix (do both):
  - (a) In SQL, resolve the zone defensively once per statement: `zones AS MATERIALIZED (SELECT name FROM pg_timezone_names)`, then `CASE WHEN u.timezone IN (SELECT name FROM zones) THEN u.timezone ELSE 'UTC' END`. Alternatively, write a small IMMUTABLE-safe helper `engagement.safe_zone(text)` that catches `invalid_parameter_value`.
  - (b) On profile write, validate against the database too (`SELECT 1 FROM pg_timezone_names WHERE name = :tz`), or map aliases to their canonical names (`Asia/Saigon` becomes `Asia/Ho_Chi_Minh`).
  - Add a test that seeds an unknown zone with admin SQL and asserts that dispatch still delivers to other users.

### High Priority

**H1. Nudges return 500 for recipients the API lets through**
- Where: src/beluno/modules/notifications.py:150-200; migration :206-215.
- Cause: the API checks the debt or the task, but not that the recipient is a different, active participant with an active account. `queue_nudge` then raises SQLSTATE 42501. Nothing maps that error, so the caller gets a raw 500 and a Sentry event.
- Probes (all raised `ProgrammingError (InsufficientPrivilege) a nudge goes between two people of the plan`):
  - P1: payment nudge to placeholder Cam, who owes Ann. This is a core flow, because placeholders are the usual debtors.
  - P2: task assigned to the organiser, nudged by the organiser (a self-nudge).
  - P3: task assigned to a placeholder.
- Same root cause, by reading the code:
  - Debtors in `left` or `removed` (SETTLING_STATES still owe money).
  - A "Former member" whose account was deleted.
  - A task assignee whose row was merged. `planning/tasks.py:134-148` treats a merged assignee as the participant it was merged into, but the nudge passes the merged row's ID.
- Fix: before calling `_nudge`, resolve the merge chain for the task assignee. Then require `access_state == 'active'`, `user_id IS NOT NULL`, user status active, and recipient different from the actor. Otherwise raise `invalid_state` (409, consistent with "done task" and "does not owe you"). Keep the DB check as a backstop.
- Product question: should nudging a placeholder be a 409 "cannot be reached", or should the button be hidden? Either way, not a 500.
- Add tests for P1-P3 plus the left/merged cases.

**H2. `queue_summaries` re-counts events every minute for three hours, without a suitable index**
- Where: migration :243-266.
- Cause: for every active-trip participant past 21:00 local time, the correlated `count(*)` runs every minute until local midnight (up to 180 times a day). It runs even after the `summary:...` row exists, because dedupe happens only at `ON CONFLICT`.
- Index: `activity.events` has `events_plan_idx (plan_id)` and `events_occurred_idx (occurred_at)`, but no `(plan_id, occurred_at)`. Each count therefore reads the plan's full event history and filters it.
- Cost scales as trips × participants × events per trip, every minute. Users are spread across time zones, so some cohort is always inside its window. This all runs inside the shared dispatch transaction that holds outbox locks.
- Fix:
  - Add `AND NOT EXISTS (SELECT 1 FROM engagement.notifications n WHERE n.dedupe_key = 'summary:'||plan||':'||user||':'||local_day)` to `people`, before counting.
  - Add `CREATE INDEX ... ON activity.events (plan_id, occurred_at) WHERE scope_type = 'plan'` (CONCURRENTLY is not possible inside the Alembic transaction; document the lock).
  - Optionally use `EXISTS` and compute the count only for the rows that get inserted.

### Medium Priority

**M1. `queue_nudge` trusts caller-supplied `p_dedupe` and entity**
- Where: migration :190-227.
- Problem: the SECURITY DEFINER function is granted to `api_runtime`. It inserts whatever `p_dedupe`, `p_entity_type` and `p_entity_id` it is given, with no namespace. An insider holding the API role (the threat model the write-guard triggers already defend against) can:
  - pre-claim other kinds' keys (`summary:<plan>:<user>:<day>`, `<event_id>:<user_id>`, `task:...`) to suppress someone's money, reminder or summary notifications;
  - vary the key to send unlimited nudges to anyone active in a shared plan;
  - attach an `entity_id` from another plan.
- Fix:
  - Build the key inside the function: `'nudge:' || p_kind || ':' || p_entity_id || ':' || actor-or-recipient || ':' || current_date`.
  - Check that for `task_nudge` the entity is a live task of `p_plan_id` whose assignee (after merges) is the recipient, and that for `payment_nudge` the entity equals the recipient participant.
  - Drop the `p_dedupe` parameter.

**M2. "Owes you" ignores the ledger's settle tolerance and suggested transfers**
- Where: notifications.py:179-186.
- Problem: the check is "my balance is above 0 and theirs is below 0 in the same currency".
  - In the base currency, `ledger_snapshot().suggestions` already applies `settle_tolerance_minor`. Balances inside the tolerance are shown as settled, yet they can still be nudged.
  - It also does not check that the debt routes to you: with A owed and B owed, C may owe only B in the suggested plan, but A can still nudge C.
- Fix: allow the nudge only if some `snapshot.suggestions[*].preview` contains a transfer from `debtor_id` to `own.id`. Otherwise, document the looser rule as a product decision. This follows the rule from earlier reviews: summary views must not re-derive "settled" outside the finance module.

**M3. `queue_reminders` lost its index range on `due_date`**
- Where: migration :147-148.
- Problem: the bound now depends on a joined column, so `tasks_due_open_idx` can no longer be range-scanned. Every minute the query reads all open, dated tasks in the system and joins them to users.
- Fix: add a time-zone-independent outer bound, `AND t.due_date BETWEEN (p_now AT TIME ZONE 'UTC')::date - 1 AND (p_now AT TIME ZONE 'UTC')::date + 2` (offsets are at most 14 hours), and keep the per-user filter.

**M4. Migration header understates the lock**
- Where: migration :20-21.
- Problem: `DROP TRIGGER` takes **AccessExclusiveLock** on `activity.events` (verified via pg_locks). That blocks activity-feed reads and every event write until the migration commits. The header says "SHARE ROW EXCLUSIVE".
- Fix: use `CREATE OR REPLACE TRIGGER events_queue_notifications ...` (PG14+), which takes only ShareRowExclusiveLock (verified). Also extend the validation block to cover `queue_summaries` (worker EXECUTE true) and to check that the trigger's WHEN includes `waiver.given`.

### Low Priority

- **L1. Nudge "once a day" uses the UTC date** (notifications.py:221, `ctx.now.date()`). In Vietnam the day resets at 07:00 local; two nudges at 06:59 and 07:01 local are both allowed. This is inconsistent with the stated default that "today" means the recipient's day.
  - The task-nudge key also has no recipient: after a same-day reassignment, the new assignee cannot be nudged.
- **L2. Waiver reversal says "payment reversed".** `reverse_settlement` emits `payment.reversed` for waivers too (finance/settlements.py:331-336). Now that `waiver_given` is pushed, the debtor sees "forgiven" and then "payment reversed" for the same item. Consider `waiver.reversed`, or mapping kind by `settlement.kind` in `fan_out`.
- **L3. Concurrent sign-out-others from two devices signs out both.** `revoke_other_sessions` (iam/sessions.py:238-249) reads without a lock. Fix: lock the user's live sessions `FOR UPDATE`, then re-check that the caller's own session is still unrevoked and raise 401 if not.
  - Not a defect: no step-up is needed. Single-session revoke has none, the action is protective, and the trigger drops push tokens on `revoked_at` (verified in 000019:175-177).
  - Each revoke writes audit and change_log via `record_mutation`, so sync parity holds. It runs one flush per session (N statements), which is acceptable.
- **L4. Sign-out-others bypasses the command runner.** The other session routes go through the runner, so this one has no `Idempotency-Key` support. It is naturally idempotent, so this is a consistency nit only.
- **L5. Nudges are allowed on archived/completed plans.** Payment nudges are allowed whenever VIEW_FINANCE allows them (this includes archived/completed plans). Cancelled plans return 403 (probe P5). Reminders skip finished plans; decide whether nudges should too.
- **L6. Summary rows are queued for people with no live device** and then marked skipped. That is harmless churn, removed after KEEP_FOR.
- **L7. Events from a merged-away guest identity count as "others".** The `actor_user_id IS DISTINCT FROM user_id` filter does not follow merges, so a person's own pre-claim guest actions inflate their own count. This is an edge case.

### Edge Cases Found by Scout / Probes
- P1-P3: unhandled 500s (H1). P4: an unknown time zone aborts all of dispatch (C1). P5: a cancelled plan returns 403 (good).
- 113 Python-valid zones (including Asia/Saigon) are unknown to Debian postgres:16 (C1).
- DROP TRIGGER takes AccessExclusive; CREATE OR REPLACE TRIGGER takes ShareRowExclusive (M4).

### Verified OK
- Trigger recreation keeps all six 000019 types and adds `waiver.given`. `queue_activity` is unchanged. A single Alembic transaction means no gap.
- `waiver.given` events use the settlement ID as `entity_id` (`_activity(..., settlement)`), so the settlements join works. Both sides are notified except the actor, and merges are followed.
- `queue_reminders`: the only differences are the time zone in the filter and in `expires_at` plus the `who` join. Plan-state and deletion filters are kept. The poll branch and dedupe keys are unchanged.
- Loc args: nudges get `[actor, plan]`, summaries `[plan, count]`, `waiver_given` (money) `[actor, plan]`, other reminders `[plan]`. They match the contract doc.
- The kind whitelist is enforced. Sender name is the plan display name only. Nudge expiry is one day. The rate limit (30/h per user) commits on its own. RLS on the outbox is unchanged. The IDOR sweep covers both new routes.
- Summary test time dependence: `evening_zone()` always yields local 22:xx, so the 21:00 check and the local-day bound are at least an hour from any edge. It is not flaky.

### Test gaps
- No test that one bad or unknown zone leaves the dispatch working (C1).
- No nudge tests for placeholder, self, left/removed, merged, or deleted recipients (H1). No test for tolerance-level balances (M2).
- Summary: no negative case before 21:00. No check that the actor's own events are excluded (the test asserts `>= 1` only). No check for non-trip or non-active plans. Dan's `summaries=False` would be skipped by the worker anyway, so the SQL filter is not proven (assert no `daily_summary` row exists via admin instead).
- `queue_reminders` time zone change is untested: no task due "tomorrow" locally but "today+2" in UTC.

### Recommended Actions
1. C1: safe zone resolution in both SQL functions, a database-backed zone check (or canonicalisation) on profile write, and a regression test.
2. H1: pre-check recipients in the API (follow merges, require active with an account, not self) and return 409, then add tests.
3. H2 and M3: dedupe before counting, a `(plan_id, occurred_at)` index, and a coarse `due_date` bound.
4. M1: derive the dedupe key inside `queue_nudge` and bind the entity to plan and recipient.
5. M4: `CREATE OR REPLACE TRIGGER` and a corrected header.
6. M2: use the snapshot suggestions, or record the looser rule as a decision.

### Metrics
- Type coverage: mypy strict clean. Lint: 0. Coverage: not run in full (targeted suites only).

### Unresolved Questions
- Which PostgreSQL build or tzdata will production use? It decides whether C1 fires on legacy aliases such as Asia/Saigon (it does on Debian postgres:16).
- Nudging placeholders, or people who left: 409, or hide the action?
- Should the nudge day follow UTC or the recipient's local day?
- Should waiver reversal get its own kind?

Status: DONE_WITH_CONCERNS
Summary: The SQL rewrites against 000019 are faithful and loc args are correct. However, any Python-valid zone that PostgreSQL lacks (Asia/Saigon on Debian PG) aborts dispatch for everyone, and nudges to placeholders, self, merged or departed people return 500.
Concerns/Blockers: Fix C1 and H1 before merge; H2 matters as soon as active trips accumulate events.
