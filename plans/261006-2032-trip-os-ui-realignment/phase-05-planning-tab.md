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

## Slice 2 design: Polls

Decisions (user, 2026-10-07):
- **A tie** in a single-choice poll closes as `tie`, listing the tied options; an organiser picks one of them when applying the outcome.
- **The electorate** is the participants active when the poll opens. It is snapshotted, so the quorum and "x of y voted" do not drift; people who join later do not vote on that poll.
- **Votes are visible** while the poll is open, including who chose what.
- **Yes/no** (after review): a poll passes when yes outnumbers no and reaches the quorum, if one is set. A quorum above the electorate is refused, and no votes at all closes as `no_votes`.
- **One tie pick** binds every outcome action of the poll (after review).

Design:
- **Schema.** Migration `000012_decisions_polls` fills the `decisions` schema:
  - `polls`: kind `single_choice` or `yes_no`, question, optional `deadline_at`, a quorum for yes/no, `allow_vote_change`, status open or closed, who closed it (null means the deadline job).
  - `poll_options`: label, optional `place_id`, position. A yes/no poll gets Yes and No options, marked by `answer`.
  - `poll_electorate`: the snapshot taken at open.
  - `poll_votes`: one row per voter, updated in place, written only by the voter themselves, and only while they are in the electorate.
  - `poll_results`: immutable, one row per poll. Holds the outcome (`winner`, `tie`, `no_votes`, `passed`, `failed`), the winner or tied options, counts, eligible and voted totals.
  - `poll_outcomes`: actions applied to a result, one per poll and action.
- **Closing.** One SECURITY DEFINER path computes and writes the result, then appends the change rows, the `poll.closed` feed event, and an audit row:
  - `decisions.close_poll` (API): the creator or an organiser;
  - `decisions.close_due_polls` (worker, every 5 minutes): polls past their deadline.

  Both lock the poll first, and a closed poll returns its existing result, so a job racing an organiser yields one result. Votes are refused once the deadline has passed.
- **Outcome actions** apply to single-choice polls and are idempotent per poll and action. The same request again returns the earlier entity; a different option gets 409.
  - `save_place` marks, or creates, the winning place as `poll_winner`.
  - `add_to_plan` creates an itinerary item from the winner, at an optional day and time.
  - Assigning a booking comes with slice 3.
- **Permissions:**
  - creating a poll needs `plan.planning.contribute`;
  - voting needs `plan.planning.respond` plus membership in the electorate;
  - closing early, deleting (open polls only), and applying outcomes take the creator or an organiser.
- **Sync, feed, and purge:**
  - plan-scope entity `poll`, with options, votes, eligible count, and result;
  - feed events `poll.created` and `poll.closed`;
  - the purge gate removes decisions rows before places.

## Slice 3 design: Bookings

Decisions (user, 2026-10-07):
- **Revealing** a booking's confirmation code and private notes is open to its travelers, its creator, and organisers. Anyone else sees them masked.
- **"Create expense from booking"** uses the existing expense API with the booking's `commitment_id`; there is no new command.
- **Assigning a booking from a poll outcome** is dropped.
- **After review:**
  - A secret left out of a save keeps its value; `null` clears it.
  - Deleting a booking clears its secrets.
  - An unchanged reference to a deleted booking or place stays valid.
  - Idempotency digests are keyed (HMAC), so stored digests never expose payload secrets.
  - Sentry never receives stack-frame variables.

Design:
- **Status:** `planned`, `confirmed`, or `cancelled`.
- **Migration `000013_bookings`** fills the `bookings` schema:
  - `bookings`:
    - kind: flight, lodging, transport, activity, restaurant, insurance, or other;
    - title, provider;
    - start and end, each a local date, optional time, and zone;
    - optional place, traveler participant IDs, status, payment note (`prepaid`, `pay_at_property`, `each_paid_own`, `personal`);
    - `free_cancellation_until`.
  - `booking_secrets`: AES-GCM ciphertexts of the confirmation code and private notes, the key ID, nonce per field, and AAD bound to the booking and field.
    - RLS lets only those who may reveal read or write it.
    - Lists, sync, logs, and feed never carry the plaintext; responses only say whether each secret is set.
  - Itinerary items gain an optional `booking_id`.
- **Keyring:** `BELUNO_BOOKING_KEYS` holds `{"active": kid, "keys": {kid: base64 32-byte key}}`. Rows remember their key, and a write re-encrypts with the active key.
- **Price** goes through `CostCommitmentPort` (`booking`, kind `price`):
  - planned → estimated;
  - confirmed → committed;
  - cancelled → cancel. An expense that already paid it stays (existing semantics).
- **Reveal:** `POST /v1/plans/{id}/bookings/{booking_id}/reveal` returns the plaintext.
  - It is not a command, so the plaintext is never stored as a replayable response.
  - It is audited and rate-limited per user.
- **Sync, feed, and purge:**
  - plan-scope entity `booking`, masked;
  - feed events `booking.added`, `booking.confirmed`, `booking.cancelled`;
  - the purge gate removes booking rows after items and before places.

## Slice 4 design: Tasks and Packing

Decisions (user, 2026-10-07):
- **Task status** (in progress, done) is changed by the assignee, the creator, or an organiser.
- **Shared packing items** can be marked packed or unpacked by everyone in the trip.
- **Packing templates come from the client:** it sends a `template_id` and the (localised) items, and the server applies each template once.

Design: migration `000014_coordination` fills the `coordination` schema.

- **Tasks:**
  - Fields: title, note, one assignee participant, due date with an optional time and zone, `remind_at` (an intent only; delivery comes in Phase 6), status `open` / `in_progress` / `done` with who completed it and when, and a link to an itinerary item or a booking (not both).
  - Content is edited by the creator or an organiser; status by the assignee, the creator, or an organiser.
- **Packing items:**
  - Fields: visibility `shared` or `private`, name, category, quantity, `bringer_participant_id` ("who's bringing this?", shared only), packed flag.
  - Private items belong to `owner_user_id`. RLS hides them from everyone else, and they sync in the owner's user scope; shared ones sync in the plan scope.
  - Shared items are added by contributors and edited or deleted by their creator or an organiser. Private items are their owner's alone.
  - "Move to shared" goes one way only.
- **Templates:** `apply_template(template_id, visibility, items)` inserts the items once per plan, owner, and template (`template_applications`); applying it again returns the existing items.
- **Feed:** `task.completed`.
- **Purge:** coordination rows are removed before items and bookings, and private items get a delete in their owner's user scope.

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
- Slice 2 (Polls) merged in PR #10; slice 3 (Bookings, sealed secrets) merged in PR #11.
- Slice 4 (Tasks and Packing) is on `feat/planning-tasks-packing`.
  - Migration `000014_coordination`; REST under `/tasks` and `/packing`, sync entities `task` and `packing_item` (private items in the owner's user scope).
  - Tests: `tests/integration/test_planning_tasks_packing.py`; the RLS, IDOR, and purge sweeps cover the `coordination` schema.
  - Gates: 597 passed, 0 skipped, coverage 95 %, OpenAPI additive.
  - Review fixes (`reports/code-reviewer-261007-1430-planning-tasks-packing-review-report.md`):
    - H1: a guest's private list moves to the account they claim;
    - H2: owners keep reading their private items after leaving, so snapshots match the feed;
    - M1: an assignee row merged into the actor's counts in the service and the guard;
    - M2: account deletion deletes private lists;
    - M3: templates insert in one flush;
    - L1: an edit without a status keeps it; L2: `completed_by_user_id` checked; L4: a task created done reaches the feed.
  - Open: re-applying a template whose items were all deleted adds nothing (by design: once per list).

