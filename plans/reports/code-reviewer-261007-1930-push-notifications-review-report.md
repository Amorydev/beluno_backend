# Code review: push notifications (uncommitted, feat/push-notifications)

## Scope
- Files: alembic/versions/000019_notifications.py, src/beluno/{push.py, modules/notifications.py, contracts/notifications.py, api/routers/notifications.py, db/models/engagement.py, worker/*, modules/account_deletion.py, config.py}, testkit/{push,database}.py, tests (integration/test_notifications.py, unit/test_push_messages.py, security/test_rls_isolation.py), docs, deploy.
- Gates run: ruff, ruff format, mypy strict: clean. OpenAPI export: in sync. Full suite (Testcontainers): 662 passed.
- Live probes (scratchpad, not committed): session purge vs push_tokens FK; an unhandled FCM error in a batch; concurrent `PUT /v1/me/push-token`. All three reproduced.
- Note: the author changed files while I reviewed (unit test renamed to test_push_messages.py at 19:24). Line numbers match the files as of 19:26.

## Overall
The privacy rules hold: loc args carry only the actor's display name and the plan title, data carries only IDs, Sentry locals are off, Android visibility is private, and the API cannot read the outbox. RLS and grants are tight. The delivery and lifecycle paths are not production-ready: one guaranteed time bomb in the auth purge, and a poison-message loop that re-sends notifications every 10 minutes forever.

## Critical

**C1. The push_tokens FK permanently breaks the auth-record purge** (000019:51; iam.purge_stale_sessions in 000010:185-205)
- `push_tokens.session_id REFERENCES iam.sessions` has no ON DELETE. The trigger removes a token only when `revoked_at` is set. An idle-expired session (90-day idle TTL) keeps its token, and 30 days after it ends `purge_stale_sessions` DELETEs the session.
- Probe result: `ForeignKeyViolation ... push_tokens_session_id_fkey`. The whole `purge_expired_auth_records` transaction rolls back, so email challenges, refresh tokens, rate-limit counters, and stale sessions stop being purged for everyone. This happens for any user who registers a device and then stops using the app for about 120 days.
- Second effect: until then, a signed-out device (expired session, never revoked) keeps receiving that account's notifications.
- Fix: the migration is not yet released, so edit 000019: `REFERENCES iam.sessions (id) ON DELETE CASCADE` (iam.users too, for consistency). At delivery, send only to tokens whose session is live: a definer function returning live tokens, or a WHERE on idle/absolute expiry. Alternatively, the purge removes tokens of expired sessions. Add an integration test: register a token, push `idle_expires_at` 40 days into the past, run `purge_expired_auth_records`.

## High

**H1. One unmapped FCM exception aborts the batch and loops forever** (push.py:92-105; notifications.py:162-170, 250-279)
- `send` catches only 6 types. Anything else escapes the send loop: ThirdPartyAuthError (a bad APNs key fails every iOS send), UnauthenticatedError, PermissionDeniedError (other than SenderIdMismatch), NotFoundError (other than Unregistered), ResourceExhaustedError (other than Quota), UnknownError, google.auth RefreshError/TransportError (credential fetches are not wrapped as FirebaseError), and ValueError from message validation.
- Once that happens, every row of the batch stays `sending`. Stuck recovery puts them back to `pending` with no attempts check, so MAX_ATTEMPTS never applies.
- Probe result (ThirdPartyAuthError on one token, 7 rounds 11 minutes apart): `[('sending',1),('sending',1)] ... [('sending',7),('sending',7)]`. Messages sent before the poison message are re-sent every round (duplicates). Messages after it are never sent.
- Fix:
  - Catch `exceptions.FirebaseError` as the base class: Unauthenticated/PermissionDenied map to a config error (retry plus alert), Unknown maps to RETRY, everything else to REJECTED. Add a final `except Exception` that maps to RETRY and logs without the token.
  - In stuck recovery, `attempts >= MAX_ATTEMPTS` should go to `failed`.
  - Better: `messaging.send_each` (up to 500, returns per-message exceptions, runs concurrently).

**H2. Slow FCM causes duplicate sends and starves the email/OTP queue** (tasks.py:206-217; push.py:88; notifications.py:31)
- Firebase defaults to `httpTimeout` 120 s plus urllib3 retries, and sends run one after another. A batch of 200 notifications times their devices can take far longer than `STUCK_SENDING` (10 min). The next dispatch then resets the in-flight rows to pending and sends them again.
- `queueing_lock` only stops a second job from waiting in `todo`. It does not stop runs from overlapping: each minute's job can start while the last one is still `doing`. Worker concurrency is 4 and is shared with the email and maintenance queues, so stacked dispatch jobs can take every slot and delay OTP emails.
- Fix:
  - Add `lock="notifications:dispatch"` to the task.
  - Pass `options={"httpTimeout": 10}` to `initialize_app`.
  - Use `send_each`.
  - Bound the wall time of each run, and keep `STUCK_SENDING` well above the worst-case run time.

## Medium

**M1. The fan-out cursor silently misses events that commit late** (000019:182-192)
- `occurred_at` is the API's `ctx.now`, captured before BEGIN (context.py:168, recorder.py:141). `until` comes from the worker's Python clock.
- An event is lost for good if its request commits more than about 2m05s after it started: lock waits, pool waits, and long sync batches all count, and there is no statement_timeout. Clock skew between API and worker hosts above about 2 minutes has the same effect.
- Fix: make it commit-ordered:
  - `activity.append_events` inserts the event id into a small `engagement.pending_events` queue in the same transaction (or inserts the notifications directly), and `fan_out` drains that queue; or
  - store `pg_current_xact_id()` on each event and only process events below `pg_snapshot_xmin(pg_current_snapshot())`.
  - At a minimum, use the DB clock for `until`.

**M2. Loc args shift position when a value is empty** (notifications.py:284; 000019:235-240)
- `_message` drops falsy args. If `actor` resolves to `''` (the actor has only a merged participant row in the plan, or no row at all) or the plan is gone, a money key built for `[actor, plan]` receives `[plan]`. The plan then appears as the actor, and on Android the missing format argument breaks localisation.
- Fix: send a fixed number of args per kind (use `""` or a neutral fallback, but keep the positions), or name the args per kind in docs/contracts.

**M3. Recipients ignore merged participant rows** (000019:199-225)
- Splits, payers, and settlements of older expenses still point at placeholder or guest rows that were later merged (`access_state='merged'`, `merged_into_participant_id`). `pp.access_state='active'` filters those people out, so edits, voids, and refunds of their expenses never reach them.
- Fix: follow `merged_into_participant_id` to the live row, the same way `ledger.settling_party` does.

**M4. Reminders ignore plan state and go stale** (000019:263-285; notifications.py:221-228)
- `queue_reminders` does not join `plans.plans`. Tasks and polls in cancelled, archived, or `deletion_scheduled_at` plans keep reminding people.
- Quiet hours postpone `poll_closing` and `task_due` past the deadline. Delivery never re-checks, so people get "poll closing" after the poll has closed and "task due" after the task is done.
- Fix: filter on plan state and `deletion_scheduled_at IS NULL`. Add an `expires_at` column to notifications (the poll deadline, the end of the due date) and skip rows that have expired.

**M5. Concurrent token registration returns 500** (000019:145-148)
- `DELETE` then `INSERT` with no lock. Two PUTs at the same moment (for example app start and a token-refresh callback) both delete, and the second INSERT then hits `UniqueViolation push_tokens_session_id_key`, giving a 500. Reproduced with `asyncio.gather`.
- Fix: inside the definer, `SELECT ... FROM iam.sessions WHERE id = p_session_id FOR UPDATE` first, and handle `unique_violation` on the token with a retry. The definer owner bypasses RLS, so `ON CONFLICT` is safe there.

**M6. Test gaps**
- The RLS sweep now excludes all four engagement tables (test_rls_isolation.py:222-225). Nothing proves that api_runtime cannot read or delete another user's token or settings, or reach the outbox or cursor.
- Missing tests:
  - unmapped or partial sender failures
  - stuck-row recovery, and `failed` once attempts reach the cap
  - NOT_CONFIGURED leading to `skipped`
  - several devices for one person
  - `payment_*` kinds
  - merged participants
  - session expiry with purge (C1)
  - cancelled or deleted plan reminders
  - the DST edge of `quiet_until`
- `device()` in the integration test always sets LOUD settings, so the default-settings delivery path is never exercised end to end.

## Low
- **L1. Reminder dates use UTC.** 000019:270 compares `due_date` with UTC dates, not the plan's timezone (so "from the day before" can mean up to about 2 days early, or nothing late on the due day in negative offsets). The dedupe key `task:id:due_date` has no user, so a reassigned task never reminds the new assignee.
- **L2. Full task scan every minute.** `coordination.tasks` has no index for the reminder query. Add a partial index on `(due_date) WHERE deleted_at IS NULL AND status <> 'done' AND assignee_participant_id IS NOT NULL`.
- **L3. Malformed tokens are never pruned.** A badly formed token answers INVALID_ARGUMENT, which maps to REJECTED, so the token stays forever (the API accepts any printable 20-4096 chars, e.g. a raw APNs token sent by mistake).
- **L4. `Message.token` is deprecated in firebase-admin 7.7** (DeprecationWarning, moving to `fid`). Track the client-side migration.
- **L5. Outbox args outlive deletions.** Args keep the actor's display name and the plan title for 30 days. Account deletion does not scrub other people's rows that name the deleted actor (deferred pending rows still deliver the old name), and plan purge leaves titles behind. Either document this in retention-matrix.md or store `actor_user_id` and scrub it.
- **L6. Web shows nothing.** `platform='web'` is accepted, but the message has no `webpush` notification, so a web client gets data only.
- **L7. Migration header overstates.** "Compatibility: additive" is not true, because the new FK changes session purge behaviour (C1). The lock note leaves out the SHARE ROW EXCLUSIVE locks the FKs take on iam.users and iam.sessions.
- **L8. Cursor edge cases.**
  - Two first runs at the same time both INSERT the cursor and one hits a PK error (retry=1 covers it).
  - After a worker outage of several days, everything since the cursor is sent as new. Cap the lookback.
- **L9. Partial delivery across devices.** If one device answers DELIVERED and another RETRY, the row becomes `sent` and the second device never gets the message. Acceptable, but document it.
- **L10. Quiet-hours input.** `quiet_start` and `quiet_end` accept times with a UTC offset. `start == end` means quiet hours are off, which is not documented. A quiet end that falls in a DST gap ends one hour late (acceptable).

## Checked and fine
- (a) Privacy:
  - No amounts or expense text in args, data, or logs; the data payload holds only IDs plus kind and category.
  - Sentry `include_local_variables=False`.
  - Android `private` only hides content when the user's lock-screen setting hides sensitive content. That is acceptable because no text carries amounts.
- (d) Delivery mechanics:
  - SKIP LOCKED, then commit `sending`, then I/O outside the transaction, then settle: correct.
  - Stuck recovery keyed on `deliver_after = now`: correct apart from H1/H2.
  - Backoff 2/4/8/16 minutes, then `failed`. Invalid tokens are deleted.
  - Settings versions: 0 before the first save, If-Match required (428), a stale version returns 412, and a race on the first save returns 412 through the savepoint.
  - quiet_until: the wrap and edge logic is correct.
- (e) Security:
  - api_runtime has SELECT/DELETE on its own tokens only, no INSERT/UPDATE (only the definer writes), and no access to the outbox or cursor.
  - The definer checks that the session belongs to the actor and is live.
  - The revoke trigger fires on logout, remote sign-out, and account deletion.
  - Token takeover needs the victim's FCM token, which the API never exposes. The effect is lost notifications and pushes for someone else's plans on the victim's device. Accepted by the "token moves" design.
- (f) Firebase setup and message shape:
  - One named app per process, cached.
  - Message shape is valid for FCM v1: string data, list[str] loc args, APNs `loc-key`/`title-loc-key`.
- (b) Event kinds and performance:
  - Kinds match ActivityType values.
  - Hangouts share `plans.plans`, so they are covered.
  - `events_occurred_idx` exists.
  - Expense joins use the revision_id primary key prefix.

## Unresolved questions
1. Should `waiver.given` notify the debtor? It is money that involves them, and it is currently left out of the kinds list.
2. Should reminders use the plan's timezone (`plans.plans.timezone`) or the recipient's?
3. Do tokens of idle-expired sessions count as "signed out" for delivery? I assume yes (C1).

Status: DONE_WITH_CONCERNS
Summary: Privacy and RLS are sound; one critical (FK blocks auth purge, reproduced) and two high delivery defects (poison message loops forever with duplicates, reproduced; slow FCM overlaps runs and starves workers) must be fixed before merge.
Concerns/Blockers: Files changed during review; re-verify line numbers after the next edit.
