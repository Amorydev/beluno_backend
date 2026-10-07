# Code review: weekly planning summary, news from the team, alembic URL precedence

Scope: `git diff feat/pdf-reports` + untracked files on `feat/weekly-news`
(000022 migration, notifications.py, push.py, scripts/news.py, alembic/env.py, tests, docs).

Gates: `ruff check` ok, `ruff format --check` ok, `mypy src` ok, target suites 31 passed
(live PG 18, nothing skipped).

Probes (scratchpad, not in repo):
- Users in America/Los_Angeles and Asia/Ho_Chi_Minh, trip in `datetime` mode in VN, one open
  poll, no tasks: VN user gets it at Sun 19:00 VN (not 18:59), LA user at Sun 19:00 PDT
  (= Mon 02:00 UTC), args `['Trip','0','1','13']`, `expires_at` = Monday 00:00 local. Correct.
- 150 pending news rows + one `expense_added` queued 5 s later: first dispatch sent 100 news,
  the money push waited for the next minute (see H1).
- `known_zone()` costs ~0.9 µs/call (100k calls 86 ms), so it is not the per-minute problem.
- FCM `MessageEncoder` output: news has only `title`/`body` (Android + APNs), other kinds only
  loc keys/args; never mixed. Encoding passes.
- `alembic current` / `upgrade --sql` from the CLI still use `BELUNO_MIGRATION_DATABASE_URL`
  (ini holds the placeholder); deploy `migrate` goes through `run_migrations`, which sets
  the same URL. No break.

## Critical

None.

## High

### H1. A news blast (and the Sunday 19:00 burst) blocks every other push for N/100 minutes
`src/beluno/modules/notifications.py:62` (`BATCH = 100`), `:329-336` (one batch per run,
`ORDER BY deliver_after, id`); `alembic/versions/000022_weekly_summary_news.py` `queue_news`
inserts one row per opted-in user, all `deliver_after = p_now`.
- Failure: news to 20k people who turned news on = 200 dispatch runs. Every expense, payment,
  nudge, or reminder queued after it waits behind it (probe: an `expense_added` slipped a full
  run behind 150 news rows). Rows with `expires_at` (nudges, task reminders, daily summary at
  midnight) can expire in the queue and get `skipped`. People with news on but no live device,
  and anyone in quiet hours, still use a batch slot each before being skipped/deferred. The weekly
  summary does the same at Sunday 19:00 for a mostly Vietnam-based user base (all in one zone).
- Fix (pick one): order due rows with news last (`ORDER BY (category = 'news'), deliver_after, id`),
  or give news a small per-run quota; and/or loop `deliver()` while it returns a full batch,
  under a time budget. Also queue news only for people with a live token
  (`engagement.live_tokens`) so dead rows never enter the queue. Add a test: >BATCH news rows
  plus one money row, money delivered in the first run.

## Medium

### M1. `queue_weekly` does the full scan every minute, 7 days a week
`000022_weekly_summary_news.py` `people`/`sunday` CTEs; called from `notifications.py:292`.
- Every minute it joins all draft/planning trips x participants x users x settings
  (`plans.plans` has no index on `state`, so it scans all plans) only to throw everything away
  outside Sunday evening. Draft trips are never closed, so this set only grows. During the 5-hour
  Sunday window, rows with nothing to say are never written, so their task/poll counts are
  recomputed for each (plan, recipient) every minute (~300 times). Lookups use `tasks_plan_idx`
  and polls `UNIQUE (plan_id, id)`, so the cost grows linearly. That is not a disaster, but it runs
  inside the shared dispatch transaction, before delivery.
- Fix: short-circuit first:
  `IF extract(isodow FROM p_now AT TIME ZONE 'UTC') NOT IN (7, 1) THEN RETURN 0; END IF;`
  (Sunday 19:00-24:00 local spans UTC Sun 05:00 to Mon 12:00 for UTC+14 to UTC-12), and count
  tasks/polls once per plan instead of once per recipient.

### M2. The news audit does not record what was sent
`src/beluno/modules/notifications.py` `send_news` audit metadata: `{key, operator, queued}`.
- Notification rows are deleted 30 days after delivery (`KEEP_FOR`), so after that nothing
  shows what was broadcast to every opted-in user. `operator` is unvalidated (can be `""`,
  whitespace, control characters).
- Fix: put both titles/bodies (they are public text) or their sha256 into the metadata, and
  `clean_text(operator)`.

### M3. The new tests never exercise the local-time logic
`tests/integration/test_weekly_summary_news.py`: all users have no time zone (UTC), timing is
`date` mode, there are no polls, and there is no case where `days` is empty.
- The main risk is not tested: zones where Sunday 19:00 local is Saturday or Monday in UTC, the
  `known_zone` fallback, `datetime` mode `starts_at` with the plan's zone, `days` null for no
  date or a date already past, the open-poll count, and completed, cancelled, or left
  participants. The probe shows the code is right today, so this is a coverage gap, not a bug.
- Fix: one test with recipients in `Asia/Ho_Chi_Minh` and `America/Los_Angeles`, a `datetime`
  trip, and an open poll, checking the exact boundary minutes (18:59 and 19:00 local).

## Low

- L1. `queue_news`: running a used key again with corrected text sends the new text only to
  people who turned news on since. The rest got the old text, and the script just prints
  "queued for N". Document this, or make the script refuse a key that already has an audit row.
- L2. Weekly/daily `expires_at` = Monday 00:00 local. Anyone whose quiet hours start at or
  before 19:00 (e.g. 18:00-08:00) never gets the weekly summary: it is deferred past expiry,
  then skipped. Daily summaries already behave this way; product call.
- L3. `alembic/env.py:23-32`: a real (non-"placeholder") `sqlalchemy.url` in a developer's
  local `alembic.ini` now beats `BELUNO_MIGRATION_DATABASE_URL`. That is intended. The sentinel
  is a substring match.
- L4. `tests/unit/test_push_messages.py` news test does not assert `title_loc_key`/`title_loc_args`
  are absent and does not run the FCM encoder. The probe confirms both are fine; one more assert
  would lock it in.

## Checked, no issue

- `loc_args` change (`notifications.py:431`): every existing producer writes strings (`actor`
  coalesced to `''`, plan title, nudge sender) or `count > 0`. `None` still gives `""`. The only
  new behaviour is `0 -> "0"`, which only `weekly_summary` produces. No regression.
- Dedupe: `weekly:<plan>:<user>:<local Sunday>` plus the unique index plus `ON CONFLICT`;
  dispatch has `lock=`. News: `news:<key>:<user>`. A rerun is idempotent (tested).
- Grants: both functions `REVOKE ... FROM PUBLIC`, granted only to `worker_runtime`. Definer
  `ON CONFLICT` follows the 000019/000020 pattern. News text: `clean_text` rejects control
  characters and blank text; the DB checks length and the slug.
- Privacy: weekly args are the plan title and counts only; Android `visibility=private`.
  News is the team's public text.
- Merged and left participants and inactive users are filtered as in `queue_summaries`.
- Docs (contract, README, runbook) match the behaviour. `scripts/` ships in the image.

## Unresolved questions

1. Should news skip people with no live device at queue time, or is a row per opted-in user
   wanted (e.g. for a future in-app inbox)?
2. Is the weekly summary meant to stop at Sunday midnight even when quiet hours delay it (L2)?

Status: DONE_WITH_CONCERNS
Summary: Weekly summary local-time logic, dedupe, grants, loc_args, and FCM building are correct (verified by probes); one High: bulk news/weekly rows block the 100-per-minute outbox for other pushes.
Concerns/Blockers: H1 should be fixed before news goes out to a real audience; M1-M3 are cheap.
