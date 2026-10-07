# ADR 0008: Trip-First Realignment

**Status:** Accepted

**Date:** 2026-10-06

**Related:** ADR 0002 (plan participant identity), ADR 0004 (offline sync)

## Context

The Stitch "Trip OS" UI defines the product as trip-first: trips, hangouts (short social plans with an optional activity icon), and crews (private saved lists of people). The backend was built with durable reusable groups, recurring plan series, and a travel extension that the client has never integrated. The schema misaligned with the UI.

## Decision

**Realign, do not rebuild.** Remove groups, plan series, travel, and group visibility; model trips and hangouts with their attributes; add crews; keep platform, identity, offline sync, and the finance ledger as-is.

### Removed
- **Groups:** durable social containers (schema, memberships, group invites, group-visible plans, group sync scope)
- **Plan series:** recurring instances; the `extend_series_horizons` job
- **Travel details:** the travel extension and segments
- **Plan visibility:** the `group_id` and `visibility` columns
- **Access levels:** `reader` (group visibility), `invited` (group membership pending)

### Added
- **Plan type:** `trip` or `hangout`, fixed at creation
- **Trip fields:** `destinations` (up to 10 ordered entries with name, code, country, dates), `pass_color` (deterministic from plan ID if omitted), `expected_size` (1–50 for "6 of 8 slots")
- **Hangout fields:** optional `activity` icon key (`dinner`, `drinks`, `karaoke`, `coffee`, `movie`, `sport`, `birthday`, `other`), no destinations
- **Member fields:** `default_share` (hundredths, default 100), `avatar_color` (self-editable for members, visible in the member palette), `capabilities` grants (`expenses.manage`, `budgets.manage`; only managers grant them, and only to participants with the `member` role; owners and admins hold both implicitly)
- **Crews:** private saved lists of people (1–50 members, owner-only), for registered accounts only: a guest cannot own one or be listed (a guest account can be retired by a claim, and guests join plans through invite links). Members must currently be active in a plan with the owner. Sync entity in the user scope. "Save from plan" captures the registered people active in it; "start from crew" reuses plan creation.
- **User default currency:** fallback for new plan base currency

### Sync scopes
Removed group scope. Remaining scopes:

| Scope | Entities |
|---|---|
| `user:{id}` | user, session, plan_access, crew |
| `plan:{id}` | plan, plan_participant, plan_invite, finance entities |

Access levels remain: `self`, `manager`, `member`.

### Contract breaks

Accepted before launch; the client had wired only health and sign-in.
`openapi/accepted-breaks.json` lists what the compatibility checker detects.
It cannot see these, so they are recorded here:

- Sync commands removed: `plan.join`, `group.*`, `series.*`, `travel.*`.
  `plan.participant.change_role` became `plan.participant.update` (role,
  default share, capabilities, avatar colour).
- Request bodies forbid unknown fields, so removed fields (`group_id`,
  `visibility`, `kind`, series and travel fields) now return `422`.
- Nested response fields went away with them (plan list items, invite preview
  and redeem responses).

## Consequences

- Fewer moving parts: queries on plans no longer branch on group visibility or series.
- Migration 000007 is forward-only; dropped objects are never restored.
- A `group:{id}` scope is no longer valid: pull and handshake reject it with `422`. No client ever synced one.
- The API keeps the `plan` name; plan operations (create, update, duplicate) now enforce type rules and new fields.
- Capabilities are a lighter alternative to the `admin` role: a manager can let a trusted member revise expenses or set budgets without promoting them.
- Money behaviour is unchanged by this decision; settling in the base currency and the FX rate source follow separately (see `plans/261006-2032-trip-os-ui-realignment/plan.md`).

## Future Decisions

- Budget categories stay a fixed list for now.
- Group and series sync history is never restored; migrations are forward-only.
- FX rate source is pending; ECB lacks VND.
