---
phase: 5
title: "Planning tab (slim)"
status: in-progress
priority: P2
effort: "3–4 weeks"
dependencies: [4]
---

# Phase 5: Planning tab (slim)

## Overview

Release 2: the trip's Plan tab (Itinerary, Places, Polls, Bookings) and More → Tasks and Packing, limited to what the screens show. Trip-only: hangouts reject planning commands. This replaces the previous plan's Phase 6, dropping availability and multiple-choice polls, the typed link graph, and the network place resolver.

## Context

- UI screens: Plan/itinerary (S84), Plan item (S62), Places (S55), Decide place (S59), Polls (S72), Polls empty (S28), Decided poll (S46), Bookings (S07), Booking detail (S68), Tasks (S13/S21), Packing (S56), Private packing (S30), Trip home "Next" (S43/S37).
- Spec: `trip-os-product-blueprint.md` §9.9–9.14, §22.5–22.6.
- Reusable: fractional ordering keys (`src/beluno/sync/ordering.py`), local time and DST handling (`src/beluno/modules/plans/timing.py`), finance `CostCommitmentPort` (callers lock the plan row first).

## Scope

- **Itinerary items:**
  - day (`date`) or anytime; optional local start time and zone, duration
  - title, place link, lead (participant), attendees (going / not going)
  - estimated cost (commitment port, `itinerary_item`), booking link, status planned/done/cancelled
  - order within a day by fractional key
  - "Attach expense": an expense revision links to the item, which converts its commitment
- **Places:**
  - name, optional Maps URL parsed offline for name and coordinates (Google, Apple, OSM); resolution state `manual`/`pending`
  - category, note, saved by
  - want-to-go reactions ("3 of 6 want to go")
  - status shortlist / in plan / poll winner; "Add to plan" creates an itinerary item
- **Polls:**
  - single choice, and yes/no with quorum ("needs 4")
  - deadline auto-close job; closing is idempotent with one immutable result
  - organiser closes early; change of vote allowed unless locked
  - outcome actions (save place, add to plan at a day and time, assign a booking) are separate idempotent commands referencing the result version
- **Bookings:**
  - type (flight, lodging, transport, activity, restaurant, insurance, other), provider/source, start/end with zones, travelers
  - price (commitment port, `booking`); payment note ("Pay at property", "Each paid own", "Personal"); free-cancellation deadline
  - confirmation code and private notes encrypted (AES-GCM keyring), masked in every projection, revealed through an audited, rate-limited endpoint
  - "Create expense from booking" converts the commitment so nothing counts twice
- **Tasks:** title, assignee, due (date or local time), status open / in progress / done, reminder intent (delivery in Phase 6), linked itinerary item or booking.
- **Packing:**
  - shared items (category, quantity, owner "who's bringing this?", packed) and private items (owner only, user-scope sync)
  - packed/unpacked intent commands; "move to shared"; built-in templates applied idempotently

## Execution Decisions (user, 2026-10-07)

- **Cancelling a booking whose cost already became an expense** is allowed, and the expense stays. The app only records money: if the provider refunds, someone records a refund on that expense (the existing refund feature), and the UI may suggest it. The same holds for an itinerary item.

  The finance port therefore leaves a converted commitment as it is when its source is cancelled, instead of refusing.
- **A paid cost whose source was withdrawn** (the item was deleted, then the expense voided) goes back to `cancelled`, not to a revived estimate (user, after review).
- **Guests and members may give their items an estimated cost** (user, after review).
- **A place goes back to the shortlist** when the last item using it is deleted (user, after review).
- **Secret reveal** (booking code, private notes): audit plus rate limit, no step-up.
- **Task assignees:** one.
- **Delivery:** four PRs, one per slice, on separate branches from `main`:
  1. Itinerary and Places
  2. Polls
  3. Bookings and encryption
  4. Tasks and Packing
- **Defaults** (not contested):
  - offline Maps URL parsing only, with no network resolver;
  - trips only: hangouts get `409 NOT_AVAILABLE_FOR_HANGOUT` (the existing code) for planning commands;
  - typed feed events for the main planning changes;
  - permissions follow the expense pattern:
    - `plan.planning.view`: every active participant;
    - `plan.planning.contribute`: owner, admin, member, and guest; viewers read only;
    - `plan.planning.manage`: owner and admin;
    - the creator of an item may edit or delete it while still holding contribute;
    - `plan.planning.respond` (want to go, going or not going): every active participant.

## Slice 1 design: Itinerary and Places

Migration `000011_planning_places` fills the `schedule_places` schema (created empty by the platform migration). Every table:
- has `plan_id` and RLS for active participants (select, insert, update);
- has no DELETE grant (deletion is a tombstone);
- has a write guard that keeps identity columns and `version + 1`;
- is added to the plan purge gate, the RLS and IDOR sweeps, and the full test tenant.

**Tables**

- `places`:
  - name, `maps_url`, `provider` (google, apple, osm), latitude and longitude, `resolution_state` (`manual`, `parsed`, `pending`);
  - category (food, sight, stay, activity, shopping, nightlife, other), note;
  - status (shortlist, in_plan, poll_winner), `saved_by_user_id`.
- `place_reactions`: `(place_id, participant_id)` with a `wants` flag. The row is updated, never deleted. Only the participant's own account writes it.
- `itinerary_items`:
  - `day` (null means anytime), `start_time` and `timezone` (both or neither), `duration_minutes`;
  - title, note, `place_id`, `lead_participant_id`;
  - status (planned, done, cancelled), `order_key` (fractional key per day, ties by ID).
- `item_attendance`: `(item_id, participant_id)` with status going or not going. Only the participant's own account writes it.

**Costs.** The item response carries `commitment_id` (clients read the synced `cost_commitment`). An item's estimated cost goes through `CostCommitmentPort` (`itinerary_item`, kind `estimate`). "Attach expense" is an expense created with the item's `commitment_id` (existing). Removing the cost, or cancelling or deleting the item, cancels the commitment unless it already became an expense.

**Commands** (REST and push):
- `place.create`, `place.update`, `place.delete`, `place.react`, and `place.add_to_plan` (creates an item and marks the place in plan);
- `itinerary.create`, `itinerary.update` (including moves via `day` and `order_key`), `itinerary.delete`, `itinerary.attend`.

**Sync.** Plan-scope entities `place` (with reaction counts and wanters) and `itinerary_item` (with attendance and its cost commitment).

**Feed.** `itinerary.item_added`, `itinerary.item_done`, `place.saved`, `place.added_to_plan`.

**Maps URLs** are parsed offline:
- Google `@lat,lng` and `q=` forms;
- Apple `ll=` and `q=`;
- OSM `mlat`, `mlon`, and `#map=`.

Anything else, short links included, stays `pending` with the name the user typed.

## Success Criteria

- [ ] Every Plan-tab, Tasks, and Packing screen renders from synced entities offline.
- [ ] Booking and itinerary costs count once in budgets (actual > committed > estimated) through the finance port.
- [ ] Private packing items never reach another participant through REST, sync, or SQL as `api_runtime`.
- [ ] Poll close races (job vs organiser) yield one result version.
- [ ] Booking codes never appear in list or sync payloads, logs, or activity events.

## Progress Notes (2026-10-07)

- Slice 1 (Itinerary and Places) is done on `feat/planning-itinerary-places`.
  - Migration `000011_planning_places`.
  - Gates: 573 passed, 0 skipped, coverage 95 %, OpenAPI additive.
  - Review fixes (`reports/code-reviewer-261007-planning-places-itinerary-review-report.md`):
    - C1: editing a costed item failed (the version now goes up before the finance port flushes);
    - H1: order keys use the C collation;
    - H2: a withdrawn paid cost voids to `cancelled` (user decision);
    - H3: Maps links are http/https only;
    - M1–M6: 500s on odd input, commitments rewritten on every save, the kill switch on no-op edits, guest authorship, the write limit, tests.
  - Open: the plan has no timezone by default, so timed items need one.
- Slices 2–4 are pending: Polls, Bookings and encryption, Tasks and Packing.

