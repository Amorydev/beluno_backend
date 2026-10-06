---
phase: 5
title: "Planning tab (slim)"
status: pending
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

## Decisions to confirm before starting

- Booking cancel when its cost already became an expense: allow and keep the expense (recommended), or `409`.
- Secret reveal: audit + rate limit (recommended), or also step-up.
- Places: offline URL parsing only (recommended), or a short-link expander with SSRF controls.
- Task assignees: one (as the UI shows) or several.

## Success Criteria

- [ ] Every Plan-tab, Tasks, and Packing screen renders from synced entities offline.
- [ ] Booking and itinerary costs count once in budgets (actual > committed > estimated) through the finance port.
- [ ] Private packing items never reach another participant through REST, sync, or SQL as `api_runtime`.
- [ ] Poll close races (job vs organiser) yield one result version.
- [ ] Booking codes never appear in list or sync payloads, logs, or activity events.
