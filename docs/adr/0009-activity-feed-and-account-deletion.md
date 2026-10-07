# ADR 0009: Activity Feed and Account Deletion

**Status:** Accepted

**Date:** 2026-10-07

**Related:** ADR 0004 (offline sync), ADR 0008 (trip-first realignment)

## Context

To complete release 1's app tabs, the People and Activity screens need to read synced data while offline. Users also need the ability to delete their accounts in compliance with store policies. Account deletion must be immediate, leave financial history intact, and show the person as "Former member" everywhere.

## Decision

**Activity feed:** an append-only `activity` schema with a `SECURITY DEFINER` gate that records events atomically with mutations in the same transaction. Events are plan-scope (seen by active participants) and user-scope (person's own account changes). Payloads carry typed summaries (IDs, amounts, roles, states, dates) never free text. Retention is 180 days via a daily worker job.

**Account deletion:** `DELETE /v1/me` after a recent sign-in (guests are exempt: their session is the only credential they have), refused with `409 OWNER_TRANSFER_REQUIRED` while the user owns a plan another person is still active in. Placeholders do not count; a guest must create an account to take ownership, or be removed. Plans owned alone are scheduled for deletion and their invite links revoked; the user's participant rows are renamed "Former member" and live non-owner ones marked `left`, and so are the names of guests merged into the account; their crews are deleted and they are removed from other people's crews; identities and email challenges are removed and sessions revoked; the profile is scrubbed with status `deleted`. Deletion is permanent.

## Consequences

### Activity Feed

- **Storage:** Append-only `activity.events` rows written via `activity.append_events()` (SECURITY DEFINER) in the same transaction as mutations. No application code writes to the table directly.
- **Schema:** `id`, `scope_type` (`plan`|`user`), `scope_id`, `plan_id` (null for user scope), `actor_user_id`, `type`, `entity_type`, `entity_id`, `summary` (jsonb object), `occurred_at`.
- **Sync entities:** Plan-scope `activity_event` visible to active participants; user-scope events only to that user. Payload: `id, type, entity_type, entity_id, plan_id, actor_user_id, summary, occurred_at`.
- **Event types (release 1):** `expense.added|edited|refunded|voided`, `payment.recorded|reversed`, `waiver.given`, `budget.changed`, `base_currency.changed`, `ledger.adjusted|consolidated|consolidation_reversed`, `kitty.contributed|withdrawn|counted`, `member.joined|left|removed|role_changed|capabilities_changed`, `guest.linked`, `plan.created|dates_changed|state_changed`, and user-scope `account.guest_upgraded|guest_merged`.
- **Summaries:** typed per event type; allowed keys are in `SUMMARY_KEYS` (amounts, currencies, roles, states, dates, field names, before/after numbers, and amounts keyed by participant ID for a ledger adjustment). Free text (descriptions, notes, names, addresses) never enters a summary; clients join IDs with synced entities.
- **Visibility:** reading a plan-scope event requires being an active participant of the plan (RLS); user-scope events are read only by that user. Writing goes through the gate, which records the session's actor and accepts plan events only from the plan's active participants (someone who just left may record only their own `member.left`, with nothing else in its summary) and user events only in the actor's own scope. Leaving, removal, and guest links are recorded only for rows that were active, so the feed never mentions someone members never saw (a pending join request that was refused or whose account was deleted).
- **Retention:** 180 days (`sync_change_retention_days`), purged by a daily `activity.purge_events()` job in batches.
- **Sync:** No REST endpoint; feed filters (All, Money, Plan, Members) are client-side.

### Account Deletion

- **Endpoint:** `DELETE /v1/me` with command `profile.delete`.
- **Step-up required:** `403 STEP_UP_REQUIRED` if session's `authenticated_at` is not fresh. Guests are exempt: they cannot sign in again, so the window would otherwise lock them out of deletion minutes after joining.
- **Ownership check:** `409 OWNER_TRANSFER_REQUIRED` if the user owns a plan another registered person or guest is still active in. Placeholders are names, not people, and never block. When only guests remain, the detail says they must create an account to take ownership, or be removed.
- **Plans owned alone:** scheduled for deletion (state machine) via `plan.deletion_scheduled`; owner row remains active. Their active invite links are locked before the ownership check and revoked, so a join either finishes first (and blocks the deletion) or finds its link gone. People who left such a plan with open balances lose that ledger when the plan is purged, as with any deleted plan.
- **Participant rows:** every participant row renamed to "Former member"; non-owner rows with `access_state` in (`active`, `pending_approval`) marked `left` with `left_at = now` and capabilities cleared (an owner row stays active, as owners must be; only plans owned alone reach this point).
- **Merged guests:** guests merged into the account keep a retired user row and merged participant rows under the guest's ID; `iam.forget_merged_guests()` renames both to "Former member" (user status `deleted`).
- **Crews:** owned crews deleted via existing deletion logic; user removed from others' crews via `people.forget_member()` (returns changed crew IDs for owners to receive change rows); empty crews are tombstoned.
- **Identities and sessions:** `iam.forget_actor_credentials()` removes user identities and email challenges; all sessions revoked with reason `account_deleted`.
- **Profile:** status set to `deleted`; email, locale, timezone, and default_currency cleared; display_name set to "Former member".
- **Money:** postings, revisions, settlements, and fund history untouched; others can settle with a `left` participant as today.
- **Audited:** with identifiers only (no profile or credential data logged).

## Implementation Notes

- `ActivityItem` refuses any summary key outside `SUMMARY_KEYS`; `tests/unit/test_activity_events.py` and `tests/integration/test_activity_feed.py` check that no description, note, or name reaches a stored summary.
- A replayed command (same idempotency key or push `operation_id`) returns its stored outcome and writes no second event. A deletion is the exception: it revokes the caller's session, so any second call, with or without the same key, gets `401`; clients treat that as done.
- The `OWNER_TRANSFER_REQUIRED` error code reuses the existing message from the `plan.leave` command, now also covering account deletion.

## Future Decisions

- Event redaction for deleted accounts (expunging summaries) is a separate data policy, not part of this implementation.
- Deletion does not yet reach data that others never see: device labels on revoked sessions, stored command responses (kept up to 180 days for replay), and the names of deleted crews. They are decided with the purge work.
- Feed search, sorting, and server-side filtering are not in scope for release 1.
