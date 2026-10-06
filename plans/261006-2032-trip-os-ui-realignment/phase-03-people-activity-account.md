---
phase: 3
title: "People, activity, and account lifecycle"
status: pending
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

## Success Criteria

- [ ] People, Person detail, Home, Settle up, and Activity render from synced data while offline.
- [ ] Activity events appear atomically with their change, never leak to removed participants, and survive lost acknowledgements (replay writes no duplicate event).
- [ ] Account deletion leaves every ledger balanced and explainable, and the person shows as "Former member" everywhere.
- [ ] Full gates green.

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Activity payloads leak sensitive text | Typed payload schemas per event and a redaction test that scans payloads for notes, codes, and addresses. |
| Feed growth | Retention floor plus compaction with change rows. |
| Deletion strands money | Deletion never touches postings; settlement with `left` participants is already supported and tested. |
