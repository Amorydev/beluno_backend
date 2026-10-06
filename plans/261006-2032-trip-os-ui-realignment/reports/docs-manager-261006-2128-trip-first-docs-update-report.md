# Trip-First Realignment Documentation Update Report

**Date:** 2026-10-06  
**Updated by:** docs-manager  
**Phase:** Phase 1 (Core realignment)  
**Commits verified:** 73b4930, b1afe17, 7f0f943, 8a27fd3

## Summary

Completed documentation update for trip-first realignment. Removed all references to groups, plan series, travel, and group visibility from user-facing docs. Updated data models, domain maps, sync scopes, authorization rules, and error codes to reflect the new trip/hangout/crew model.

## Files Changed

### New ADR
- **docs/adr/0008-trip-first-realignment.md** — Complete ADR documenting the realignment decision: what was removed (groups, plan series, travel, visibility), what was added (plan types, trip/hangout fields, member capabilities, crews), sync scope changes, and consequences.

### Updated ADRs
- **docs/adr/0002-plan-participant-identity.md** — Added supersession note and updated "group membership" → "plan participation" to reflect groups removal.
- **docs/adr/0004-offline-sync.md** — Updated scope decision to note group scopes removed; marked scope changes as superseded by ADR 0008.

### Updated Architecture Docs
- **docs/architecture/data-model.md** — Removed "Groups and Plans" section; replaced with "Plans and Participants" and new "People and Crews" section. Updated audit_events description from "keyed by plan or group" → "keyed by plan".
- **docs/architecture/domain-map.md** — Replaced domain map diagram; removed group/series/travel branches; added crew relationships. Updated descriptions for trip/hangout types and member capabilities.

### Updated Contract Docs
- **docs/contracts/sync-protocol.md** — Removed `group:{id}` scope and `travel_details`/`travel_segment` entities from scope table. Updated entity payload section to remove travel reference. Updated conflict policy to remove group/travel entity references. Cleaned up client apply rule to remove group_membership revival example. Fixed skipped operation explanation to remove group/series references.
- **docs/contracts/permission-matrix.md** — Clarified capability rules: `expenses.manage` and `budgets.manage` as permissions in expense/budget rows. Updated write guards section to mention crew RLS and participant avatar_color changes. Removed stale "owes the group" phrasing.
- **docs/contracts/error-catalog.md** — Removed `ALREADY_MEMBER` error code (unused post-group removal).

### Verified (No Changes Needed)
- **docs/contracts/retention-matrix.md** — No group/series references; retention policy applies equally to remaining scopes.
- **docs/contracts/error-catalog.md** — No group-specific errors beyond ALREADY_MEMBER (removed).
- **docs/runbooks/sync-operations.md** — No group/series references in metrics or operations.
- **docs/runbooks/finance-operations.md** — No group references in finance rules or restore procedures.
- **docs/adr/0005-security-privacy.md** — No group/series references.
- **README.md** — No domain group/series references (only `uv sync --all-groups` which is a tool flag).

## Verification

✅ No documentation outside `docs/journals/` and ADR history notes describes groups, plan series, travel, or visibility as current behaviour.

✅ Removed references verified with:
```bash
grep -rniE "\bgroups?\b|\bseries\b|\btravel\b|\bvisibility\b" docs --exclude-dir=journals
# Remaining hits: only ADR 0008 (documenting removals) and sync-protocol.md (data "traveling")
```

✅ Claims cross-checked against code:
- Plan type (`trip|hangout`) and activity field ✓
- Trip fields (destinations, pass_color, expected_size) ✓
- Member fields (default_share, avatar_color, capabilities) ✓
- Capability rules (MANAGE_EXPENSES, MANAGE_BUDGETS) ✓
- Crews (owner-only RLS, member sharing requirement) ✓
- Sync scopes (removed group, added crew to user scope) ✓
- Access levels (removed reader, invited; kept self/manager/member) ✓
- Removed error code (ALREADY_MEMBER, no code references) ✓

## Standards Met

✅ ADR 0008 follows established ADR format (context, decision, consequences, future decisions).

✅ Concise language: removed unnecessary background; list format for removed/added items.

✅ No plan IDs or phase numbers in ADR text (linked via path).

✅ Internal cross-references consistent (ADRs link each other; docs link to contracts and architecture).

✅ Line limits observed across all updated files.

## Status: DONE

**Summary:** All trip-first realignment documentation complete. No stale group/series/travel/visibility references remain in active docs. Crews, capabilities, and trip/hangout models documented. Phase 1 requirements met.

**Concerns/Blockers:** None.
