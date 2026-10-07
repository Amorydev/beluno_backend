---
phase: 3
title: "People, activity, and account lifecycle"
status: completed
priority: P1
effort: "1–1.5 weeks"
dependencies: [1, 2]
---

# Phase 3: People, activity, and account lifecycle

## Overview

Complete release 1's app tabs:

- **People:** per-person balances per group, without netting across groups.
- **Activity:** a readable, cross-group feed of what changed.
- **Me:** account deletion ("you become 'Former member'"), which store policies require once accounts can be created.

Home's "Needs you" is computed on the client from synced entities.

## Context

- UI screens: People (S29), People empty (S85), Person detail (S00), Crew detail (S45), Activity (S40/S75), Home (S61), Me (S49).
- Spec: `trip-os-product-blueprint.md` §8.1, §9.19, §9.22, §18.7.
- Crews ship in Phase 1; this phase adds the read side the tabs need.

## Requirements

### Settlement suggestions in the ledger entity

- The `ledger` sync entity embeds the current deterministic settlement suggestions per currency (same greedy algorithm as the preview, with the Phase 2 tolerance).
- Person detail ("An owes you 747,768 ₫ · Japan 2027"), Home balances, and Settle up then work offline.
- Never netted across plans or currencies.

### Activity feed

- **Storage and sync:**
  - Append-only `activity.events` rows written in the `record_mutation` seam, in the same transaction as the change.
  - Plan-scope sync entity `activity_event`, visible to the plan's active participants.
  - User-scope events for the person's own account ("An linked their guest profile to an account").
- **Event types for release 1:**
  - Money: expense added, edited (with a field summary), refunded, voided; payment recorded, reversed; waiver; budget changed; base currency changed; consolidation; kitty contribution, withdrawal, count.
  - Members: joined, left, removed, role or capability changed, guest linked to an account.
  - Trip: created, dates changed, state changed (ready to settle, settled).
- **Payload:** IDs, versions, and a small structured summary (amounts and before/after values allowed, since members already see them). Never notes, codes, or addresses.
- Retained 180 days (same floor as change rows).
- Feed filters (All, Money, Plan, Members) are client-side.

### Account deletion

- `DELETE /v1/me` with step-up.
- Refused with `409 OWNERSHIP_TRANSFER_REQUIRED` while the user owns a plan that has other active participants.
- Otherwise:
  - revoke all sessions and remove identities
  - scrub the profile (no email; display name "Former member")
  - rename the user's participant rows to "Former member" and mark active ones `left`
  - delete owned crews and remove the user from others' crews
  - keep financial history intact
- Others can still settle with a former member (existing `left` rules).
- Audited with identifiers only.

### Default currency and profile

Already in Phase 1. Me → Export my data moves to Phase 6.

## Execution Decisions (user, 2026-10-07)

- **Account deletion is immediate**: one transaction, no grace period.
- **Plans the person owns alone** (no other active participant) are scheduled for deletion through the existing plan deletion state; hard purge comes with Phase 4 retention.
- **Branch:** `feat/people-activity-account`, stacked on PR #6 (money alignment).

After review (user, 2026-10-07):

- **Placeholders never block** an owner's deletion: a plan with only the owner and placeholders counts as owned alone. **Guests do block**, with a detail saying they must create an account to take ownership, or be removed.
- **Guests skip step-up** when deleting: their session is the only credential they have.
- **Open balances of people who left** a plan owned alone do not block deletion; that ledger goes with the plan's purge, as with any deleted plan.
- **Owner ledger adjustments** appear in the feed as `ledger.adjusted` (currency and amounts per participant, never the memo).

## Design

### Settlement suggestions

- `LedgerSnapshot` carries the plan's base currency and the suggestions per currency, computed with the same function as `GET /ledger/settlement-preview` (tolerance included). The `ledger` entity gains `suggestions`; every balance, tolerance, or base change already bumps the ledger version.

### Activity

- New schema `activity`, table `activity.events` (append-only): `id`, `scope_type` (`plan`|`user`), `scope_id`, `plan_id`, `actor_user_id`, `type`, `entity_type`, `entity_id`, `summary jsonb`, `occurred_at`.
- `record_mutation` takes an optional `ActivityItem(type, summary)`; the seam buffers the event with the audit and change rows and writes it in `flush_pending_records`, plus one `activity_event` change row in the same scope. A replayed command returns its stored outcome and runs nothing, so it writes no duplicate.
- Summaries are typed per event type: IDs, roles, states, amounts and currencies, field names, before/after numbers and dates. **No free text** (descriptions, notes, names, codes, addresses); clients join IDs with synced entities. A redaction test checks every summary key against the allowed set.
- Visibility: plan-scope events for the plan's active participants (RLS `plans.actor_is_active_participant`); user-scope events only for that user.
- Retention: a worker purge (180 days, `sync_change_retention_days`) next to change-row compaction.
- Event types (release 1): `expense.added|edited|refunded|voided`, `payment.recorded|reversed`, `waiver.given`, `budget.changed`, `base_currency.changed`, `ledger.consolidated|consolidation_reversed`, `kitty.contributed|withdrawn|counted`, `member.joined|left|removed|role_changed|capabilities_changed`, `guest.linked`, `plan.created|dates_changed|state_changed`, and user-scope `account.guest_upgraded|guest_merged`.

### Account deletion

- `DELETE /v1/me` (command `profile.delete`), step-up required (`403 STEP_UP_REQUIRED` otherwise).
- `409 OWNER_TRANSFER_REQUIRED` (the code `leave` already uses) while the person owns a plan with another active participant.
- Otherwise, in one transaction:
  1. Schedule deletion of the plans they own alone.
  2. Rename every participant row to "Former member" and mark active rows `left`.
  3. Tombstone their crews; a SECURITY DEFINER gate removes them from other people's crews and returns the crews it changed, so their owners get change rows.
  4. Remove identities and email challenges.
  5. Scrub the profile: no email, locale, timezone, or default currency; display name "Former member"; status `deleted` (a new value).
  6. Revoke every session.
- Postings, revisions, and settlements stay untouched. Others settle with a `left` participant as today.
- Audited with identifiers only.

## Success Criteria

- [x] People, Person detail, Home, Settle up, and Activity render from synced data while offline (backend side: the ledger entity carries suggestions, the feed and participants sync; the client screens are not built yet).
- [x] Activity events appear atomically with their change, never leak to removed participants, and survive lost acknowledgements (replay writes no duplicate event).
- [x] Account deletion leaves every ledger balanced and explainable, and the person shows as "Former member" everywhere.
- [x] Full gates green.

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Activity payloads leak sensitive text | Typed payload schemas per event and a redaction test that scans payloads for notes, codes, and addresses. |
| Feed growth | Retention floor plus compaction with change rows. |
| Deletion strands money | Deletion never touches postings; settlement with `left` participants is already supported and tested. |

## Completion Notes (2026-10-07)

Built on `feat/people-activity-account`: ledger suggestions, the activity feed, account deletion, a rate-limit test fix, docs, and review fixes. Gates: ruff/format/mypy clean, full suite on real PostgreSQL green (0 skipped), coverage 95 %, OpenAPI exported and compatible. Migration `000009_activity_and_deletion`.

Deviations, decided during implementation:

- The owner row of a plan owned alone stays `active` (renamed only): owners must be active, and the plan is scheduled for deletion anyway.
- Deletion reuses `409 OWNER_TRANSFER_REQUIRED` (the code `leave` uses) instead of a new `OWNERSHIP_TRANSFER_REQUIRED`.
- No REST feed endpoint: the feed is read through sync only, with client-side filters.
- 26 event types: the release-1 list plus `ledger.adjusted` (added after review).

Review fixes (`reports/code-reviewer-261007-people-activity-account-review-report.md`):

- Guests merged into a deleted account kept their guest-era name on the retired user and its merged rows; a definer gate (`iam.forget_merged_guests`) now renames both.
- Invite joins could race the "owns alone" check; deletion now locks the owned plans' live invite links before counting and revokes them, so a join either finishes first and blocks the deletion or finds its link revoked (tested with a held lock).
- The feed write gate admitted removed and pending people; it now takes plan events only from active participants, plus a leaver's own exact `member.left`.
- Leaving, removal, and guest links are recorded only for rows that were active (no feed noise about pending applicants).
- The person's participant rows are locked during deletion.
- A replayed `DELETE /v1/me` answers 401 (the session is gone); documented as done.
- Pending applicants cannot withdraw (`leave` returns 404 for them), so that review scenario does not occur.

Accepted, documented:

- Deletion does not yet reach data others never see: device labels on revoked sessions, stored command responses (180 days), names of deleted crews. Phase 4 retention decides them.
- Pending join requests in a plan owned alone stay unanswered once it is scheduled for deletion.
- The feed gate trusts active participants' summary shape as it trusts their other writes; `ActivityItem` enforces the allowed keys in the API.

Reports: `reports/code-reviewer-261007-people-activity-account-review-report.md`, `reports/tester-261007-people-activity-account-gates-report.md`, `reports/docs-manager-261007-0948-people-activity-account-docs-report.md`.
