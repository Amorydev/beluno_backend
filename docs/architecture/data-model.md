# Data Model

## Core Principles

- UUIDv7 IDs are client-generatable and sortable.
- PostgreSQL is the cross-device authority; client SQLite is a UI-local source.
- `amount_minor BIGINT` plus currency metadata represents money.
- Timed data stores UTC instants plus IANA timezones; date-only data uses SQL `date` and never fake midnight UTC.
- Mutable roots use optimistic versions and soft-delete tombstones.
- Every plan-scoped relationship is constrained by `plan_id` and stable `plan_participant_id` where applicable.

## Identity and Authorization

The `iam` schema holds users, sessions, and refresh-token digests:

- **users**: both registered and guest accounts; guests are upgraded in place when they claim an identity. Status: `active`, `disabled` (a guest merged into another account), or `deleted` (the person deleted their account; permanent, signing in again creates a new account); deleted accounts hold no email, locale, timezone, or default currency, and their display name is "Former member"; guests merged into a deleted account are renamed the same way and marked `deleted`.
- **sessions**: one per device, with 90-day idle and 180-day absolute TTL; logout and device revocation are immediate. Revoke reason: `logout`, `user_revoked`, `refresh_reuse`, `account_merged`, or `account_deleted`.
- **refresh_tokens**: stored only as HMAC-SHA256 digests; single-use and rotation-guarded (theft detection).
- **email_challenges**: OTP codes and magic links for passwordless sign-in; delivery by Procrastinate worker.
- **user_identities**: links Google and Apple provider accounts; requires explicit account linking (no email-only matching).
- **passkeys**: WebAuthn credentials of registered accounts (credential ID, COSE public key, signature counter, transports, backup state, a label); added after a recent sign-in, used to sign in and step up; deleted with the account. No attestation is kept.
- **webauthn_challenges**: single-use registration and sign-in challenges stored as HMAC digests, valid five minutes, bound to the caller (registered or guest) when signed in; used up even when the response fails or the sign-in is refused. Passkeys are inserted and deleted only by their owner (RLS), and only their counter, backup state, last use, and label can be updated.

Access tokens are 15-minute ES256 JWTs (/.well-known/jwks.json), signed with keys from `BELUNO_AUTH_SIGNING_KEYS`; every request re-checks session and account state in PostgreSQL.

## Plans and Participants

The `plans` schema models individual plans (trips or hangouts):

- **plans**: trips (destination-based, up to 10 stops) or hangouts (activity-based, optional icon). Each has a type (fixed at creation), base currency, state machine (draft → planning → active → settling → completed), and optional expected size.
- **plan_participants**: stable historical identities for votes, money, and RSVP; guest claims link to users without rewriting history. Members hold a default share (default 1.0×), a visible avatar color, and optional capabilities (`expenses.manage`, `budgets.manage`).
- **plan_invites**: bearer tokens for joining or claiming a placeholder; optional email binding, use limit, and guest switch.

## People and Crews

The `people` schema holds each user's private, saved lists of people:

- **crews**: a name and `member_user_ids` (1–50 registered users), owned by one registered user. A newly listed person must currently be active in a plan with the owner (`people.crew_write_guard` backs the API check). Owner-only under RLS; deletes are tombstones (`deleted_at`). Clients start new plans from a crew.

## Activity

The `activity` schema holds an append-only feed of what changed in plans and user accounts:

- **events**: immutable rows written only through `activity.append_events()` (SECURITY DEFINER), which records the session actor and validates authorization. Each event carries: `id` (UUIDv7), `scope_type` (`plan` or `user`), `scope_id` (plan ID or user ID), `plan_id` (plan for plan-scope events, null for user-scope), `actor_user_id` (who made the change), `type` (e.g., `expense.added`, `member.joined`), `entity_type` (e.g., `expense`, `plan_participant`), `entity_id`, a typed `summary` object (structured keys only: amounts, currencies, roles, dates, field names; never free text), and `occurred_at`.
- Visibility: plan-scope events are readable by the plan's active participants (RLS `plans.actor_is_active_participant`); user-scope events only by that user. The write gate accepts plan events only from the plan's active participants (someone who just left may record only their own `member.left`) and user events only in the actor's own scope.
- Retention: 180 days (`sync_change_retention_days`), purged by a daily `activity.purge_events()` worker job in batches.

## Trip planning

The `schedule_places` schema holds a trip's places and itinerary (trips only):

- **places**: name, optional Maps link (read offline for coordinates: `manual` without a link, `parsed` when the link carried coordinates, `pending` otherwise), category, note, status (`shortlist`, `in_plan`, `poll_winner`), who saved it. Tombstoned on delete.
- **place_reactions**: one row per participant ("want to go"), updated in place.
- **itinerary_items**: a day (or anytime), an optional local start time with its IANA zone (stored as local time and zone), duration, title, note, place, lead, status (`planned`, `done`, `cancelled`), and a fractional `order_key` within the day. Tombstoned on delete.
- **item_attendance**: one row per participant (going / not going), updated in place.
- A place is `in_plan` while a live itinerary item uses it and back on the `shortlist` otherwise (a poll winner keeps its status). "Want to go" and attendance count active participants only.
- An item's estimated cost is a finance cost commitment (`itinerary_item`, `estimate`); the expense that pays it names that commitment. Cancelling or deleting the item cancels a cost that is still only planned; a paid one stays an expense and remembers the withdrawal (`converted_from_state = cancelled`), so voiding that expense cancels the cost rather than reviving it. An item save writes the commitment only when the cost or title changed. Order keys use the `C` collation.
- RLS: the plan's active participants read and write; there is no DELETE grant. Write guards keep identities fixed and let each participant write only their own reactions and attendance.

### Polls

The `decisions` schema holds a trip's polls:

- **polls**: `single_choice` or `yes_no` (passes when yes outnumbers no and reaches the optional quorum, which cannot exceed the electorate; no votes at all closes as `no_votes`), question, optional deadline, whether votes may change, status `open`/`closed`, who closed it (null: the deadline). After opening only its version moves; whoever manages it (creator or organiser) may withdraw it while open.
- **poll_options** (label, optional place, position; yes/no options carry `answer`) come from the creator before the poll opens; `decisions.open_poll` then snapshots **poll_electorate** (participants active at opening, placeholders excluded) once and seals the options.
- **poll_votes**: one per voter, updated in place; the database accepts only the voter's own vote, from the electorate, on an option of the poll, while open and before the deadline.
- **poll_results**: one immutable row written by `decisions.finalize_poll` (outcome `winner`, `tie`, `no_votes`, `passed`, `failed`; counts; eligible and voted). The API (`decisions.close_poll`, creator or organiser) and the worker (`decisions.close_due_poll`, every 5 minutes) both close through it, locking the poll first, so a race yields one result.
- **poll_outcomes**: one per poll and action (`save_place`, `add_to_plan`), naming the option and the place or item it made. A tie is settled once: every action of the poll uses the option picked first. A free-text winner becomes one place, and its itinerary item points at it.

### Bookings

The `bookings` schema holds a trip's reservations:

- **bookings**: kind (flight, lodging, transport, activity, restaurant, insurance, other), title, provider, start and end (local date, optional time in an IANA zone), optional place, traveler participant IDs (participants of the plan, checked by a guard), status `planned`/`confirmed`/`cancelled`, payment note, free-cancellation deadline, and whether each secret is set. Only the creator or an organiser changes one (a guard enforces it). Tombstoned on delete; items keep pointing at a deleted booking, and an unchanged reference stays valid. Itinerary items may point at one (`booking_id`).
- **booking_secrets**: the confirmation code and private notes sealed with AES-256-GCM (fresh nonce, associated data binding the row and field) under a key from `BELUNO_BOOKING_KEYS`; rows remember their key so rotation keeps old ones readable. RLS (`bookings.actor_may_reveal`) lets only the travelers (a traveler merged into another participant counts as that participant), the creator, and the plan's owner/admins read or write them. A save that leaves a secret out keeps it (resealed under the active key); null clears it. Deleting a booking clears both.
- The price is a finance cost commitment (`booking`, `price`): estimated while planned, committed once confirmed, cancelled with the booking unless an expense already paid it (that expense stays).

### Tasks and packing

The `coordination` schema holds a trip's to-dos and packing lists:

- **tasks**: title, note, one optional assignee (an active participant), a due date with an optional local time in an IANA zone, a reminder time (recorded only; delivery comes with notifications), status `open`/`in_progress`/`done` (`completed_at` and `completed_by_user_id` set exactly while done), and an optional link to an itinerary item or a booking (not both). The creator or an organiser edits or deletes one (an edit that names no status keeps it); the assignee, counting a participant row merged into theirs, may change only its status, and whoever completes it is recorded as themselves (a guard enforces both). Tombstoned on delete; an unchanged link stays valid after its target is deleted.
- **packing_items**: name, category, quantity 1–99, packed, optional template ID, and a visibility. Shared items belong to the trip: anyone in it marks them packed, the creator or an organiser edits them, and one may name who brings it. Private items have an owner: RLS hides them from everyone else, only the owner touches them (and still reads them after leaving the trip), and they sync in the owner's user scope. A private item may become shared, never the reverse. A guest who claims an account brings their private items along; deleting an account deletes them.
- **template_applications**: one row per (plan, owner or the shared list, template ID). Templates come from the client already localised; applying one again returns the items already there instead of adding duplicates.

## Finance

The `finance` schema holds each plan's ledger (ADR 0003). Amounts are `BIGINT`
minor units of a currency pinned in `finance.currencies`.

- **plan_ledger_heads**: one per plan; `ledger_seq`, status (`open|settled|reopened`), open dispute count, money settings (`count_personal_spend`, `settle_tolerance_minor`), base-currency change count; every finance write locks it after the plan row.
- **ledger_accounts / account_balances**: one account per participant and currency plus a fund account per currency; balances are a synchronous projection.
- **expenses / expense_revisions / expense_payers / expense_splits**: stable identity plus immutable revisions with raw split input, `lr-v1` resolved shares, an optional base-currency snapshot, revision origin (`http|sync`), optional occurred time and timezone, and base-currency change number.
- **expense_refunds / refund_shares**, **settlements** (payments and waivers), **fund_settings / fund_movements** (target per member), **fund_counts** (kitty stocktakes), **fx_snapshots**.
- **ledger_transactions / ledger_postings**: the journal; every transaction sums to zero per currency and carries the next `ledger_seq`. Transaction kinds: `expense`, `expense_reversal`, `refund`, `settlement`, `settlement_reversal`, `fund_contribution`, `fund_withdrawal`, `conversion`, `conversion_reversal`, `adjustment`.
- **ledger_confirmations**: append-only; each participant confirms the ledger at one sequence.
- **budgets** and **cost_commitments** (finance-owned; other modules use the `CostCommitmentPort`); commitments carry base-currency change number.
- **base_currency_changes**: append-only numbered rate per plan (old-to-new).
- **consolidations / consolidation_rates / consolidation_lines**: a consolidation row (`active`, then possibly `reversed` once) with append-only rates (one frozen FX snapshot per converted currency) and lines (per participant and currency: the balance moved and the base amount it became).
- **market_rates** (reference table, append-only, worker-only insert): daily rates for offline estimates.

Canonical rows are append-only (triggers reject UPDATE/DELETE); deferred
constraint triggers verify sums and posting shapes at commit; RLS limits every
row to active participants of its plan; the worker reaches finance data only
through SECURITY DEFINER reconciliation gates.

## Media

`media_memories.media` records each uploaded file of a plan: kind (`receipt` linked to an expense that was not voided, `cover`, `memory` with a caption, local day and time, an optional saved place, and an organiser's `in_recap` pick), state (`awaiting_upload` -> `scanning` -> `ready`, or `rejected` with `type`, `size`, `malware`, `unreadable`, or `missing`), the declared type and size, and once clean the stored type, size, and pixel size. Bytes live in object storage under `incoming/{id}` (until scanned) and `media/{id}`; rows hold no file names. A guard lets the API add a file in its uploader's name, move it to scanning (the uploader), edit a memory's details (the uploader or an organiser), pick highlights (organisers), or delete it (the uploader, an organiser, and for a receipt the expense's creator or a holder of `expenses.manage`); only the worker settles it. Account deletion removes the person's memories (`media_memories.forget_memories`); receipts stay. `media_memories.object_deletions` queues keys (with the time they become due) for the worker to delete from storage; a definer trigger fills it when a file is deleted, the plan purge when a plan goes, and the worker for objects it no longer needs. The API has no access to it. `plans.plans` has `cover_media_id` (a ready cover of the plan) and `album_url`.

## Notifications

The `engagement` schema holds `push_tokens` (one FCM token per session, removed when the session is revoked or purged; only live sessions are reached, through `engagement.live_tokens`), `notification_settings` (categories and quiet hours; no row means the defaults), and the `notifications` outbox (one row per person and reason, deduplicated by key, with a localisation key, safe arguments, and for reminders an expiry). A trigger on `activity.events` queues money events in `fan_out_queue` within the same transaction; definer functions `engagement.fan_out` (following merged participants) and `engagement.queue_reminders` (plans still being organised) fill the outbox, so the worker never reads money or plan tables.

## Support

`analytics_ops.problem_reports` keeps what people send from "Report a problem":
category (`balance_wrong`, `sync_issue`, `other`), an optional plan and linked record
(IDs only, no foreign keys so a report outlives a purged plan), the person's words,
and an optional diagnostic code with its snapshot (identifiers, states, versions,
sync sequences, the count of ledger disagreements from `finance.reconcile_plan`
through `finance.actor_ledger_problems`). The API inserts in the actor's own name
and reads nothing; the worker role reads for operators (`scripts/support.py`, each read
audited as `support.problem_report_read` with the operator). Account deletion removes the
person's reports and those of guests merged into the account
(`analytics_ops.forget_problem_reports`).

## Security and Audit

All table rows have RLS policies (SECURITY DEFINER helper functions):

- **RLS**: enforces tenant and participation boundaries; all writes pass through application authorization logic first.
- **Write guards**: tenant-aware triggers (000003_tenant_write_guards.py) prevent bulk operations and orphaned changes.
- **sync_audit.audit_events**: immutable log of user actions (created, modified, deleted), keyed by plan.
- **sync_audit.change_log**: pointer rows (`scope_type`, `scope_id`, `scope_seq`, entity, version, operation) with a contiguous per-scope sequence assigned by `sync_audit.append_changes` at commit; the source of cursor-based pull.
- **sync_audit.scope_heads**: per-scope last sequence, compaction floor, and generation (bumped after a restore).
- **sync_audit.operations**: stored outcomes of idempotent commands keyed by `(actor, command, idempotency_key)`, kept 180 days.
