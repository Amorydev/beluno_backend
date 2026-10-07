# ADR 0002: Stable Plan Participant Identity

**Status:** Accepted

**Updated:** 2026-10-06 (ADR 0008 supersedes group membership; plan models now trip-first)

`PlanParticipant` is the stable reference for plan records. Plan participation is
current access only. A guest can claim a verified user account, and removed
participants lose access while history, balances, and audit attribution remain
valid.

## Update (2026-10-06)

Groups and group membership have been removed (ADR 0008: trip-first realignment).
Plan access is now through plan invites and direct participant addition only.
The stable participant identity and guest claim mechanics remain unchanged.
