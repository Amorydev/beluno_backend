---
phase: 6
title: "Planning and Coordination"
status: pending
priority: P1
effort: "5 weeks (2 backend engineers, parallel with Phase 5 + fractional QA/security review)"
dependencies: [4]
---

# Phase 6: Planning and Coordination

## Context Links

- [Plan overview](./plan.md)
- [Phase 3: Identity, Groups and Plans](./phase-03-identity-groups-plans.md)
- [Phase 4: Reliability and Sync Kernel](./phase-04-reliability-sync-kernel.md)
- [Phase 5: Financial Ledger](./phase-05-finance-ledger.md) — parallel workstream; integrate only through its approved cost-commitment contract.
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- [Product blueprint](../beluno-product-blueprint.md) — use the canonical `Group → Plan → PlanParticipant` model.
- PostgreSQL date/time types: <https://www.postgresql.org/docs/current/datatype-datetime.html>
- OWASP SSRF prevention: <https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html>

## Overview

Implement the collaborative planning graph used by every plan: schedule/itinerary items, saved places, structured polls, bookings, packing lists, and lightweight responsibilities. Every object is plan-scoped, works offline through the Phase 4 command/change protocol, uses stable `PlanParticipant` references, and supports links between decisions, places, activities, bookings, and assignments without becoming a chat app, route optimizer, booking marketplace, or general project manager.

This phase runs in parallel with Phase 5. It owns planning facts and booking lifecycle, while finance remains the sole owner of commitments, expenses, budgets, and actuals. The two workstreams share a versioned contract; neither module queries or writes the other's tables.

## Requirements

### Functional

- Create, update, reorder, move, duplicate, complete, cancel, archive, and tombstone schedule items for timed, date-only, and unscheduled activities.
- Save places from manual input or supported external links, resolve metadata asynchronously, preserve an offline fallback, record shortlist status/reactions, and link places to polls, schedule items, or bookings.
- Support single-choice, multiple-choice, yes/no approval, and availability polls with explicit electorate, quorum, deadline, vote-visibility, vote-change, close, tie, and outcome rules.
- Create and manage manual bookings for lodging, transport, flights, activities, restaurants, insurance, and other reservations, including participants, local times, refundability, sensitive confirmation data, and links to schedule/responsibility records.
- Support shared and private packing items, built-in template application, assignment, quantity/category, and idempotent packed/unpacked intent commands.
- Support lightweight responsibilities with one or more assignees, due date/time, status, linked planning object, and reminder intent; omit subtasks, dependency graphs, boards, and general project-management features.
- Emit typed domain events and atomic audit/change/outbox records so notifications, finance, media, exports, memories, and recap can consume planning state in later dependency waves.

### Non-functional

- Route every mutation through Phase 4 idempotency, authorization, optimistic versioning, tombstone, audit, change-log, and outbox primitives.
- Scope every relation with composite `plan_id` foreign keys; a bare object or participant ID must never create a cross-plan link.
- Use PostgreSQL `date` for date-only facts and UTC instants plus explicit IANA start/end timezones for timed facts; never derive authoritative order or deadlines from device time.
- Use opaque fractional-order keys with deterministic ID fallback and a server-owned rebalance command; do not store ordering in floating-point numbers.
- Make poll close/result calculation, booking lifecycle transitions, template application, and reorder/rebalance concurrency safe and deterministically replayable.
- Keep confirmation codes and private notes encrypted and excluded from ordinary list, sync, log, analytics, notification, and public-share payloads.
- Bound titles, notes, option counts, list sizes, batch sizes, coordinates, URLs, and provider payloads before expensive parsing or background work.

## Architecture

### Module ownership and canonical entities

```text
plans.Plan ──< schedule.ScheduleItem ──< ScheduleAttendee
     │                  │
     │                  └── optional ──> places.Place
     ├──< places.Place ──< PlaceReaction
     ├──< decisions.Poll ──< PollOption ──< PollVote
     ├──< bookings.Booking ──< BookingParticipant
     └──< coordination.PackingItem
     └──< coordination.Responsibility ──< ResponsibilityAssignee

PollOutcome ── explicit command ──> Place | ScheduleItem | Booking draft
Booking ── application port ──> finance.CostCommitment
Booking/Schedule/Place/Poll ── typed links ──> Responsibility
```

- `schedule_places` owns `schedule_items`, attendees, places, provider-resolution state, reactions, lane/order keys, and their read models.
- `decisions` owns poll electorate, options, ballots, close/result rules, and outcome snapshots. It does not own the resource created from an outcome.
- `bookings` owns reservation facts and sensitive booking metadata. It requests finance commitments through a port and later media/reminder work through outbox contracts.
- `coordination` owns packing and responsibilities. It may reference planning entities only through plan-scoped, typed link tables.
- Module services may compose inside one application transaction, but modules never perform ad hoc cross-module table reads or writes.

Suggested tables include `schedule_items`, `schedule_attendees`, `places`, `place_reactions`, `place_resolution_attempts`, `polls`, `poll_eligible_participants`, `poll_options`, `poll_votes`, `poll_results`, `bookings`, `booking_participants`, `booking_secrets`, `planning_links`, `packing_items`, `responsibilities`, and `responsibility_assignees`. Every mutable root has `plan_id`, integer `version`, lifecycle timestamps, actor metadata, and `deleted_at`; every participant relation references `(plan_id, participant_id)`.

### Command and event flow

```text
REST or sync push command
  -> Phase 4 operation executor reserves/replays idempotency key
  -> current-state plan policy authorizes actor and visibility
  -> module locks root/lane/poll/booking as required
  -> validate version, links, time, lifecycle and bounds
  -> persist canonical rows + audit_event + change_log + outbox
  -> store canonical response and commit
  -> workers resolve place metadata or fan out downstream events
```

Representative domain events are `schedule_item_created|moved|completed`, `place_saved|resolved`, `poll_opened|vote_changed|closed`, `booking_created|changed|cancelled`, `packing_item_marked`, and `responsibility_assigned|completed`. Events contain safe IDs, versions, and coarse state only. Sensitive fields are loaded by an authorized consumer at execution time, never copied into a generic outbox.

### Time, schedule, and ordering model

- A schedule item has explicit `time_mode=timed|date_only|unscheduled`. Timed items store derived UTC start/end plus the original local date/time and IANA start/end timezone; date-only items use `date` fields and no fake midnight instant.
- Reject nonexistent DST local times and require an explicit offset/fold choice for ambiguous times. End must be valid after start in instant terms; cross-timezone and overnight records remain legal.
- Schedule lanes use `(plan_id, lane_kind, lane_date)` and an opaque fractional key. Inserts choose a key between neighbors; ties sort by UUIDv7/ID. When keys exceed a configured length or density threshold, an idempotent lane-locked rebalance rewrites keys and emits one ordered change set.
- Delete-versus-edit follows Phase 4 tombstone-wins semantics. Duplicate creates a new ID with copied safe fields and fresh links/versions.

### Place resolution and external content

- Offline create accepts a user-entered name and optional external URL. The record is immediately usable with `resolution_state=pending|manual`; provider metadata is optional enrichment.
- The resolver accepts only configured map/place providers and normalized HTTPS URLs. It resolves DNS/IP at fetch time, blocks private/link-local/loopback ranges and redirect escapes, enforces response/time/byte limits, and never renders provider HTML.
- A worker records provider namespace, external ID, sanitized address/category, bounded coordinates, provenance, and attempt state. Version checks prevent late provider results from overwriting newer user edits.
- Deduplication only suggests a merge; it never silently coalesces user-authored places. Coordinates and exact locations inherit sensitive-plan visibility.

### Poll semantics

- On open, persist the eligible participant set or an explicit `electorate_mode` and snapshot denominator so quorum cannot drift when membership changes. Removed users lose read/write access, but their historical ballot treatment follows the approved rule.
- Uniqueness matches poll type: one selected option for single choice, one row per voter/option for multiple choice, one approval value per voter, and one availability value per voter/slot.
- `allow_vote_change=false`, deadline, maximum selections, anonymous-to-peers mode, quorum numerator/denominator, and deterministic tie policy are server-enforced. "Anonymous" never means unaudited or hidden from privileged abuse investigation.
- Closing locks the poll, uses server time, computes a versioned immutable result snapshot, and is idempotent across scheduler/manual-close races. Creating a schedule item/place/booking draft from the winner is a separate authorized idempotent command referencing the result version.

### Booking and finance boundary

- Booking state is `draft|confirmed|changed|cancelled|completed`; transitions validate required fields and preserve revision/audit history. Confirmation codes and restricted notes live in encrypted columns separated from ordinary booking projections.
- Start/end support separate local timezones, date-only reservations, and provider-local identifiers. Participants are stable plan participants; removal from the plan does not rewrite history.
- Booking price/currency/refundability are planning facts. `bookings` sends `upsert`, `cancel`, `refund`, or `convert` intent through the versioned finance cost-commitment port using `(plan_id, booking_id, booking_version)` as the dedup source identity.
- Phase 6 contract tests use an in-memory/recording adapter while Phase 5 is built in parallel. The combined adapter must commit booking mutation and finance commitment atomically before the Phase 7 integration gate; there is no direct finance table access or duplicated budget total in planning.
- Once a commitment is converted to an expense, a booking edit/cancel cannot erase or silently reduce the actual. The finance port returns the allowed transition or a stable conflict that requires an explicit refund/reversal workflow.

### Packing, responsibilities, and visibility

- Shared packing items are visible to active permitted participants; private items are owner-only in policy, RLS, snapshots, pull pages, exports, and notification payloads.
- `mark_packed` and `mark_unpacked` are intent commands ordered by accepted server sequence. Replaying the same operation is harmless; stale version conflicts return the current state instead of trusting device timestamps.
- Applying a built-in template snapshots bounded item data under a deterministic application ID, allowing retry without duplicate items. Templates contain no executable or user-supplied markup.
- Responsibilities allow multiple assignees and `open|in_progress|done|cancelled`; completion/cancellation records actor and time. Due values follow the same timed/date-only rules as schedules. Links use approved typed relations and plan-scoped FKs.
- Assignment or reminder events do not send directly. They enter an outbox; delivery later rechecks membership, visibility, quiet hours, and preferences.

## Proposed File Inventory

All paths are proposed and `[UNVERIFIED]` until the backend repository exists.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/src/beluno/modules/schedule_places/{schedule,places}/*` `[UNVERIFIED]` | Schedule lanes/items and safe place resolution | Unit/integration/security |
| Create | `backend/src/beluno/modules/decisions/{polls,outcomes}/*` `[UNVERIFIED]` | Poll rules, ballots, result snapshots | Unit/concurrency |
| Create | `backend/src/beluno/modules/bookings/*` `[UNVERIFIED]` | Booking lifecycle, secrets, downstream ports | Integration/security |
| Create | `backend/src/beluno/modules/coordination/{packing,responsibilities}/*` `[UNVERIFIED]` | Lists, assignments, due-state transitions | Unit/integration |
| Create | `backend/src/beluno/contracts/{planning,decisions,bookings,coordination}.py` `[UNVERIFIED]` | Pydantic REST/sync/event schemas and errors | OpenAPI/compatibility |
| Create | `backend/src/beluno/db/models/{schedule_places,decisions,bookings,coordination}.py` `[UNVERIFIED]` | SQLAlchemy mappings/queries | Type/integration |
| Create | `backend/alembic/versions/000040_planning_coordination.py` `[UNVERIFIED]` | Tables, composite FKs, indexes, RLS SQL | Migration/security |
| Create | `backend/src/beluno/worker/tasks/resolve_place.py` `[UNVERIFIED]` | Allowlisted asynchronous metadata resolution | SSRF/fault tests |
| Create | `backend/src/beluno/worker/tasks/close_poll.py` `[UNVERIFIED]` | Deadline close/result calculation | Clock/concurrency |
| Create | `backend/tests/integration/test_planning_concurrency.py` `[UNVERIFIED]` | Reorder, vote, close, toggle, tombstone races | Integration |
| Create | `backend/tests/security/test_planning_visibility.py` `[UNVERIFIED]` | IDOR/private packing/booking secret coverage | Security |
| Create | `backend/tests/e2e/test_planning_graph.py` `[UNVERIFIED]` | Poll-to-place-to-schedule-to-booking flow | E2E |

Phase 2 module/OpenAPI registration and Phase 4 command/change dispatch will be modified after their concrete paths are verified. The Phase 5 finance adapter is integrated only through its published contract. No files are deleted.

## Implementation Steps

1. Freeze planning object state machines, time modes, visibility rules, poll electorate/quorum/tie behavior, booking transitions, link types, ordering algorithm, and stable error catalogue in ADRs/contracts.
2. Write golden unit tests for DST gaps/folds, cross-timezone intervals, date-only values, order-key insertion/rebalance, poll result rules, and packing/responsibility intent transitions.
3. Add planning/coordination tables with composite plan-scoped FKs, uniqueness/check constraints, tombstones, encrypted-secret separation, indexes, RLS, grants, and migration rollback compatibility.
4. Implement schedule commands and read models for timed/date-only/unscheduled items, attendance, move/copy/cancel/complete, lane pagination, fractional ordering, and deterministic rebalance.
5. Implement manual place creation, reactions/status, typed links, URL normalization, allowlisted resolver jobs, provenance, retry/backoff, late-result version checks, and offline fallback.
6. Implement poll create/open/vote/change/close/reopen-if-policy-allows with electorate snapshots, quorum, anonymous-peer views, server deadlines, result snapshots, and outcome-action commands.
7. Implement booking commands, participant links, time/refundability fields, encrypted secret reveal, lifecycle revisions, and safe list/detail projections.
8. Publish and contract-test the finance commitment consumer port. Add recording adapter tests now and validate the real Phase 5 adapter without importing finance persistence code.
9. Implement shared/private packing lists, built-in template snapshotting, assignment, quantities/categories, and idempotent packed/unpacked intent commands.
10. Implement responsibilities, multi-assignees, due modes, typed links, assignment/status transitions, and safe domain events for reminders.
11. Register all commands with Phase 4 push dispatch, include every object in snapshot/delta/tombstone flows, and define redacted change payloads for private/restricted fields.
12. Add OpenAPI endpoints, cursor pagination, expected-version handling, rate limits, audit fields, observability, and worker DLQ/replay procedures.
13. Run real-PostgreSQL migration/RLS tests, deterministic-clock concurrency tests, multi-device fault models, cross-plan IDOR tests, SSRF tests, and production-shaped list/order load tests.

## Todo

- [ ] Planning state machines, time semantics, visibility, ordering, and typed-link rules are approved.
- [ ] Schedule items support timed, date-only, unscheduled, cross-timezone, attendance, and deterministic ordering flows.
- [ ] Place saves work offline and provider resolution cannot reach unapproved networks or overwrite newer edits.
- [ ] All poll types enforce electorate, quorum, selection, deadline, anonymity, close, tie, and outcome rules deterministically.
- [ ] Booking lifecycle and restricted fields are complete without exposing secrets in list/sync/telemetry payloads.
- [ ] Finance commitment contract tests prevent double-counting and forbid direct finance-table access.
- [ ] Shared/private packing visibility and idempotent status intents work across multiple devices.
- [ ] Responsibilities support multiple assignees, due modes, typed links, and safe reminder events.
- [ ] Every mutation emits atomic audit, change, and outbox records and survives lost acknowledgements.
- [ ] OpenAPI, sync snapshots/deltas, RLS, dashboards, alerts, and runbooks cover every planning module.

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | Actor substitutes another plan's place, participant, booking, or poll ID | Denied without existence leakage; composite FK prevents stored cross-plan link |
| Critical | Private packing item is requested in list, pull, export, notification, or by service role | Only owner/explicitly approved privileged path can see it; no metadata leak |
| Critical | Booking create commits but response is lost and offline client retries | Canonical response replays; one booking and one cost-commitment intent only |
| Critical | Poll deadline worker and organizer close the same poll concurrently | One immutable result version; identical deterministic outcome and one event |
| Critical | Booking converted to actual expense is later cancelled offline | No silent actual deletion/double count; explicit finance conflict/refund path |
| High | Activity crosses DST fold/gap or starts/ends in different timezones | Gap rejected; fold disambiguated; intended local values and UTC ordering preserved |
| High | Two devices insert/move items between identical neighbors during rebalance | All items remain once; deterministic total order converges after pull |
| High | Removed participant tries to vote, reveal code, complete task, or receive reminder | Current-state policy denies; history remains attributed and delivery suppresses |
| High | Malicious place URL redirects to loopback/private IP or returns huge content | Resolver blocks before fetch/follow, records safe failure, and leaves manual place usable |
| High | Membership changes after a quorum poll opens | Approved electorate snapshot rule applies; denominator/result does not drift silently |
| High | Two assignees complete/reopen the same responsibility offline | Intent/version rules yield one current state with complete audit and no duplicate event |
| Medium | Applying the same packing template twice after timeout | Same operation replays without duplicate items; a new explicit application may copy again |
| Medium | Provider resolution returns after user changes place name/address | Enrichment does not overwrite user-authored newer version; provenance remains inspectable |
| Medium | Client edits an item that another device tombstoned | Tombstone wins; stable recreate/copy response does not resurrect old ID |

## Success Criteria

- [ ] Real-PostgreSQL tests prove composite tenant FKs, RLS, uniqueness, tombstones, version checks, and atomic audit/change/outbox behavior.
- [ ] Model tests converge under duplicated, dropped, reordered, delayed, and lost-ack planning commands and change pages.
- [ ] Golden time fixtures cover date-only, unscheduled, overnight, DST gaps/folds, and different start/end timezones.
- [ ] Concurrent reorder/rebalance tests preserve every item exactly once and produce a deterministic order without unbounded key growth.
- [ ] Poll model/property tests cover every type, electorate change, quorum boundary, tie, deadline, anonymous-peer response, and scheduler race.
- [ ] Booking secrets never appear in ordinary OpenAPI examples, logs, traces, analytics, list projections, change payloads, or notification events.
- [ ] Finance contract tests prove one booking source maps to at most one effective commitment tier and that planning stores no budget aggregate.
- [ ] Private/shared packing and responsibility permissions pass the complete role/state/visibility matrix, including worker/service roles.
- [ ] Place resolver SSRF, redirect, DNS rebinding, content-size, timeout, provider-outage, and DLQ/replay tests pass.
- [ ] Schedule, poll, booking, packing, and responsibility queries meet the agreed Phase 2 SLO at production-shaped plan sizes.

## Risk Assessment

- **Generic plan drift toward travel-only assumptions:** bookings and itinerary concepts can force destinations/dates. Keep `Plan` generic, make every module optional, and test dinner/movie/sports plans alongside trips.
- **Ordering key explosion or divergent lanes:** frequent offline moves can densify keys. Use opaque bounded keys, deterministic fallback, lane locks, and a rehearsed server rebalance.
- **Poll ambiguity:** dynamic electorate, ties, or changed votes can make decisions unexplainable. Snapshot rules and persist immutable result inputs/output/version.
- **Booking/expense double counting:** parallel modules may duplicate ownership. Finance exclusively owns commitments/actuals; enforce source uniqueness and contract tests.
- **Sensitive itinerary/booking disclosure:** generic sync/events can leak exact location or codes. Use minimum payloads, separate encrypted secrets, object-level policy, and redaction tests.
- **External place-provider abuse:** URL resolution creates SSRF, quota, and untrusted-data risks. Allowlist providers/networks, bound resources, sanitize outputs, and degrade to manual data.
- **Coordination scope creep:** comments, chat, dependencies, boards, and routing multiply complexity. Enforce the approved lightweight state machines and typed links only.

## Security Considerations

- Re-authorize current plan participation, role, object visibility, ownership, and lifecycle inside every command and worker action; possessing an ID, cursor, outbox event, or signed link grants no authority.
- Apply plan-scoped composite FKs/RLS to every attendee, vote, assignee, participant, and link. Explicitly test API, worker, migration, and service roles for RLS bypass.
- Encrypt confirmation codes/restricted notes with managed key rotation and audited reveal; never place decrypted values in cache keys, search, telemetry, notification bodies, or generic audit payloads.
- Treat exact schedules, coordinates, traveler lists, and availability ballots as sensitive. Anonymous ballots are hidden from peers but retain controlled auditability.
- Validate/sanitize text and URLs, neutralize formula/CSV injection before export, cap JSON depth/count/bytes, and prevent outbound resolver access to internal networks.
- Rate limit votes, place resolution, reorder/rebalance, template application, secret reveal, and reminder-triggering changes by actor/device/plan; return `Retry-After` without renumbering queued operations.

## Rollback and Exit Gate

Deploy additive tables/columns, readers, and command generations before enabling new writes. Keep the prior sync generation through the compatibility window. On a defect, disable the affected command or provider resolver with a kill switch while preserving authorized reads, tombstones, audit, and queued operations. Do not roll back by hard-deleting planning history, revealing encrypted fields, resetting poll outcomes, or rewriting order from client timestamps. Rebuild disposable read models from canonical rows and repair bad links only through audited forward commands/migrations.

Exit requires deterministic time/order/poll proofs, cross-plan and private-data security suites, booking-secret redaction, SSRF controls, lost-ack and multi-device convergence tests, finance-port contract compatibility, production-shaped query/load results, dashboards/alerts, worker replay procedures, and rollback rehearsal. Phase 7 may integrate notifications, media, imports, exports, memories, and recap only after this gate and the Phase 5 finance gate both pass.
