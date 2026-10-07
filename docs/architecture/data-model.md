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

## Security and Audit

All table rows have RLS policies (SECURITY DEFINER helper functions):

- **RLS**: enforces tenant and participation boundaries; all writes pass through application authorization logic first.
- **Write guards**: tenant-aware triggers (000003_tenant_write_guards.py) prevent bulk operations and orphaned changes.
- **sync_audit.audit_events**: immutable log of user actions (created, modified, deleted), keyed by plan.
- **sync_audit.change_log**: pointer rows (`scope_type`, `scope_id`, `scope_seq`, entity, version, operation) with a contiguous per-scope sequence assigned by `sync_audit.append_changes` at commit; the source of cursor-based pull.
- **sync_audit.scope_heads**: per-scope last sequence, compaction floor, and generation (bumped after a restore).
- **sync_audit.operations**: stored outcomes of idempotent commands keyed by `(actor, command, idempotency_key)`, kept 180 days.
