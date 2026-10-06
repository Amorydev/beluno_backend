---
phase: 1
title: "Architecture Kernel"
status: in-progress
priority: P1
effort: "2 weeks (2 backend engineers + fractional product/security review)"
dependencies: []
---

# Phase 1: Architecture Kernel

## Context Links

- [Plan overview](./plan.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- [Product blueprint](../beluno-product-blueprint.md) — canonical Beluno product source using `Group → Plan → PlanParticipant`.
- PostgreSQL transaction isolation: <https://www.postgresql.org/docs/current/transaction-iso.html>
- PostgreSQL row security: <https://www.postgresql.org/docs/current/ddl-rowsecurity.html>
- FastAPI application structure: <https://fastapi.tiangolo.com/tutorial/bigger-applications/>
- SQLAlchemy asyncio: <https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html>
- Alembic migrations: <https://alembic.sqlalchemy.org/en/latest/>
- Procrastinate task queue: <https://procrastinate.readthedocs.io/en/stable/>

## Overview

Freeze the domain language, invariants, module boundaries, API/sync contracts, security model, and operational objectives before implementation. The deliverable is an executable architecture contract for one unified backend, not production code and not a reduced release slice.

The product model is deliberately generic:

- `Group`: reusable circle of people and defaults across many plans.
- `Plan`: one dinner, coffee, movie, match, birthday, custom event, or multi-day trip.
- `PlanParticipant`: stable plan-scoped identity used by historical records even after a user leaves, a guest claims an account, or group membership changes.
- `TravelPlanDetails` and travel segments: optional extension for travel-only fields.
- `plan_kind`: presentation/default selector only; it must not branch authorization, finance, sync, or lifecycle into separate products.

## Requirements

### Functional

- Catalogue all domain commands, events, read models, state machines, and cross-module links for the full product scope.
- Define stable identities for authenticated users, guest identities, group memberships, plan participants, and guest claim/merge.
- Define plan lifecycle without requiring travel fields; date-only, timed, recurring-series, and multi-day plans must all fit.
- Define plan-level RSVP independently from access/role, including invited, going, maybe, declined, and removed states.
- Define safe plan duplication/reuse: copy selected structure/defaults, never balances, ledger history, private media, booking secrets, or revoked access.
- Define money, split, FX, fund, settlement, booking commitment, budget, media, notification, export, deletion, and sync invariants.
- Produce OpenAPI and sync-envelope examples sufficient for mobile/web teams to build against mocks.
- Produce permission, data-classification, retention, audit, abuse, RPO/RTO, and provider-degradation matrices.

### Non-functional

- Prefer a modular monolith over microservices; boundaries must still be enforceable in imports, schemas, ownership, and tests.
- All authoritative state changes happen through the API/application service; Supabase client credentials never authorize domain-table writes.
- Use UTC instants plus IANA timezone for timed events and PostgreSQL `date` for date-only values.
- Use UUIDv7 for client-generatable sortable IDs, `BIGINT` minor units for money, and `NUMERIC` for FX ratios.
- Require deterministic behavior for rounding, ordering, retries, and conflict outcomes.
- Proposed launch objectives: database RPO ≤5 minutes/RTO ≤4 hours; media RPO ≤1 hour/RTO ≤24 hours; 90-day supported offline duration; 180-day minimum idempotency/tombstone retention. Validate these assumptions before Phase 2.

## Architecture

### Deployment and runtime decision

One Python application package is deployed in one write region in three roles:

```text
Mobile/Web clients
       │ HTTPS REST + OpenAPI
       ▼
FastAPI/Pydantic API ─────────────┐
       │                          │
       ▼                          ▼
Supabase PostgreSQL ◄── Procrastinate worker
       │                          ▲
       ├── change_log/outboxes ───┘
       ├── Supabase Auth
       └── Supabase Storage quarantine/ready buckets

Scheduler role enqueues periodic jobs; SSE/WebSocket may emit invalidation hints,
but cursor-based change_log remains the only durable synchronization contract.
```

Do not introduce GraphQL, Kafka, Redis, Kubernetes, separate services, multi-region writes, or a general event-sourcing platform initially. Revisit only after measured constraints.

### Module ownership

| Module | Owns | May depend on |
|---|---|---|
| `iam` | user profile link, device/session metadata, policy subject | platform only |
| `groups` | groups, memberships, reusable group defaults | iam |
| `plans` | plans, participants, invites, travel extension, lifecycle | iam, groups |
| `sync_audit` | idempotency, operations, change log, audit | iam, plans contracts |
| `finance` | expenses, revisions, splits, ledger, FX, budgets, fund | plans, sync_audit |
| `decisions` | polls/options/votes | plans |
| `schedule_places` | itinerary/activity and place shortlist | plans |
| `coordination` | packing and responsibilities | plans |
| `bookings` | reservations and cost commitments | plans, finance contracts |
| `media_memories` | upload state, attachments, memories | plans |
| `engagement` | notifications, preferences, delivery | domain-event contracts |
| `search_export` | derived search documents, exports, recap | read-only contracts |
| `billing` | entitlements/provider receipts | iam, plans identifiers |
| `analytics_ops` | consented product events/support diagnostics | public event contracts |

Modules do not query another module's tables ad hoc. Cross-module writes use application ports inside the same transaction; asynchronous effects use transactional outboxes.

### Aggregate and identity decisions

- `GroupMembership` represents current reusable-group access; it is not a historical foreign key for plan content.
- Creating a plan snapshots selected people into `PlanParticipant`; a participant has `identity_kind=user|guest|placeholder`, optional `user_id`, immutable ID, display snapshot, state, role, and join/leave metadata.
- Guest claim links the same participant to a verified user atomically; it does not replace participant IDs or rewrite history.
- Tables that reference a participant use composite foreign keys such as `(plan_id, participant_id)` to prevent cross-plan linkage.
- Removing a participant revokes access but preserves finance/audit/history references. One current owner is required until a recovery workflow explicitly intervenes.
- `TravelPlanDetails(plan_id)` is one-to-one and optional; destinations/segments belong to the extension. Generic schedules remain available to every plan.
- `PlanSeries` optionally owns a validated recurrence rule and timezone; it materializes independent `Plan` instances with stable occurrence keys, explicit exceptions, and no shared mutable ledger.
- Participant access state, role, and RSVP response are separate fields; declining attendance never silently revokes access or destroys historical participation.
- Duplicate/reuse commands use an explicit copy manifest. Financial history, settlements, fund state, private content, confirmation secrets, and audit history are excluded by default.

### ADR set to approve

1. Modular-monolith and single-write-region deployment.
2. Group/Plan/PlanParticipant identity and guest claim semantics.
3. Permission decision function: actor × membership/participant state × role × resource visibility/owner × plan/resource state × action.
4. Financial signs, posting accounts, revisions/reversals, split algorithm versions, fund semantics, FX direction/rounding, and cost commitment double-count prevention.
5. Operation idempotency, entity versions, conflict matrix, change-log cursor, tombstone/compaction/full-resync semantics.
6. Date/timezone and fractional ordering policies.
7. Data classes, field encryption, logging/analytics redaction, upload quarantine, deletion and retention.
8. SLOs, capacity envelope, RPO/RTO, backup/restore, degradation and kill switches.
9. Backward-compatible API/database/sync evolution and minimum supported client policy.

## Proposed File Inventory

All paths are proposed because no codebase exists; every entry is `[UNVERIFIED]` until the repository is scaffolded.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/docs/architecture/system-context.md` `[UNVERIFIED]` | Runtime/deployment/data-flow decision | Architecture review |
| Create | `backend/docs/architecture/domain-map.md` `[UNVERIFIED]` | Boundaries, ownership, dependency rules | Import-boundary tests later |
| Create | `backend/docs/architecture/data-model.md` `[UNVERIFIED]` | Group/Plan/Participant canonical model | Schema review |
| Create | `backend/docs/adr/0001-modular-monolith.md` `[UNVERIFIED]` | Architecture rationale | ADR gate |
| Create | `backend/docs/adr/0002-plan-participant-identity.md` `[UNVERIFIED]` | Stable identity/guest claims | Identity scenarios |
| Create | `backend/docs/adr/0003-financial-ledger.md` `[UNVERIFIED]` | Money and ledger invariants | Property-test source |
| Create | `backend/docs/adr/0004-offline-sync.md` `[UNVERIFIED]` | Sync and conflicts | Model-test source |
| Create | `backend/docs/adr/0005-security-privacy.md` `[UNVERIFIED]` | Policies/data lifecycle | Security matrix |
| Create | `backend/docs/adr/0006-operations-recovery.md` `[UNVERIFIED]` | SLO/RPO/RTO | Restore gate |
| Create | `backend/docs/contracts/permission-matrix.md` `[UNVERIFIED]` | Deny-by-default rules | Policy suite |
| Create | `backend/docs/contracts/error-catalog.md` `[UNVERIFIED]` | Stable machine codes | Contract suite |
| Create | `backend/docs/contracts/sync-protocol.md` `[UNVERIFIED]` | Push/pull envelopes | Sync suite |
| Create | `backend/docs/contracts/retention-matrix.md` `[UNVERIFIED]` | Lifecycle/compaction | Deletion suite |
| Create | `backend/openapi/openapi.yaml` `[UNVERIFIED]` | Initial API contract | Lint + compatibility |

No files are modified or deleted in this phase because the backend repository does not yet exist.

## Implementation Steps

1. Run a domain workshop using concrete plans: dinner tonight, coffee poll, weekly football match, birthday, and 10-day multi-currency trip. Reject any required field or invariant that only works for travel.
2. Write the ubiquitous-language glossary and aggregate ownership table. Resolve `Group`, `Plan`, `PlanSeries`, `Participant`, `Member`, `Guest`, `Owner`, `Attendee`, `RSVP`, and `Payer` ambiguities.
3. Model lifecycle state machines for group membership, participant access, plan, invite, poll, booking, task, upload, expense revision, settlement, export, notification, and deletion.
4. Specify the permission matrix and tenant-scoping rule for every resource/action, including removed participants and background/service roles.
5. Specify the data-classification and retention matrix; explicitly reject identity/passport uploads until a compliant workflow exists.
6. Specify money and ledger invariants, supported split types, currency exponent source, amount bounds, FX snapshots, settlement choices, and fund-not-custody wording.
7. Specify durable operation/change envelopes, cursor/high-watermark semantics, conflict rules, error taxonomy, compaction floor, and client version handshake.
8. Define REST resources, commands, pagination, idempotency headers, optimistic concurrency, stable error shape, and example OpenAPI schemas.
9. Define transactional event/outbox ownership, worker retry/DLQ semantics, provider boundaries, and safe-degradation rules.
10. Define SLOs, sizing assumptions, rate-limit starting points, backup topology, restore verification, and launch gates.
11. Review ADRs jointly with mobile/web, product, QA, security/privacy, and operations stakeholders; record owners and due dates for unresolved assumptions.
12. Freeze the architecture baseline. Later changes to money, sync, identity, or permission contracts require a new ADR and migration/compatibility review.

## Todo

- [ ] Approve ubiquitous language and root aggregate diagram.
- [ ] Approve Group → Plan → stable PlanParticipant model and guest claim semantics.
- [ ] Approve RSVP/access separation, recurrence materialization, exception, and plan-copy rules.
- [ ] Approve module dependency map and ban on cross-module table access.
- [ ] Approve permission matrix and tenant-scoped composite-FK strategy.
- [ ] Approve financial, split, FX, fund, cost commitment, and settlement invariants.
- [ ] Approve sync envelope, conflict matrix, retention, compaction, and full-resync policy.
- [ ] Approve API/error/versioning contract and generated-client plan.
- [ ] Approve data classification, upload policy, deletion ledger, and analytics/log redaction.
- [ ] Approve SLO, capacity, rate-limit, RPO/RTO, and provider-degradation assumptions.
- [ ] Sign all ADRs; assign every unresolved assumption an owner and deadline.

## Test Scenario Matrix

| Priority | Scenario | Expected architecture outcome |
|---|---|---|
| Critical | Guest joins dinner, later claims account, then joins a trip with same group | Existing participant IDs remain stable; no duplicated finance/history identity |
| Critical | Group member removed after paying an expense | Access revoked; participant/history and balance remain valid |
| Critical | Trip-specific destination data absent on movie plan | All generic plan flows remain valid; no nullable-travel branching in core |
| Critical | Weekly football series crosses DST and one occurrence is cancelled | Stable local time is preserved; only the selected occurrence is cancelled; no duplicate occurrence |
| Critical | Organizer duplicates a completed trip as a dinner plan | Only allowlisted structure/defaults copy; no ledger, balance, private media, booking secret, or old access leaks |
| High | Invitee declines RSVP but later reviews an expense involving them | RSVP and access remain distinct; historical participant and authorized financial view stay valid |
| Critical | Lost HTTP response after committed financial command | Same operation/key replays canonical response and creates no duplicate |
| High | Two devices edit an expense at same version | Explicit version conflict; no LWW or partial ledger update |
| High | Booking becomes paid expense | One cost commitment transitions from planned/committed to actual without double count |
| High | Client cursor predates tombstone floor | Server returns `FULL_RESYNC_REQUIRED`; client does not replay blindly |
| Medium | Realtime invalidation is lost | Cursor pull still converges; realtime is optional hint only |
| Medium | FX/media/notification provider unavailable | Core write remains correct; outcome is stale/manual, quarantined, or queued according to contract |

## Success Criteria

- [ ] Every module, table family, command, and event has one named owner.
- [ ] Product, client, backend, QA, and security reviewers approve the nine ADR topics.
- [ ] `Group`, `Plan`, `PlanParticipant`, and travel extension support every example without travel-root assumptions.
- [ ] RSVP, recurring series/exceptions, and plan duplication have explicit state machines and privacy-safe copy manifests.
- [ ] Permission and data-classification matrices cover 100% of planned API actions and background roles.
- [ ] OpenAPI and sync/error examples are internally consistent and lintable.
- [ ] No money field is ambiguous about minor units, currency, FX direction, or rounding.
- [ ] SLO/RPO/RTO, retention, launch capacity, and provider decisions have owners and test methods.
- [ ] Phase 2 can scaffold without reopening a P0 domain decision.

## Risk Assessment

- **Generic-model overreach:** a universal event schema can become vague. Mitigate with explicit domain extensions and scenario walkthroughs, not a JSON catch-all.
- **Stable-identity mistakes:** merging users and participants can corrupt history. Keep participant identity immutable and make claim/link a separately audited operation.
- **Architecture ceremony without proof:** ADRs may hide ambiguity. Require executable contract examples and scenario matrices.
- **Underestimated scope:** full backend is large. Keep one product scope but enforce dependency waves, WIP limits, and honest exit gates.
- **Managed-service assumptions:** Supabase features and backup behavior vary by tier. Validate limits/contracts before Phase 2 procurement.

## Security Considerations

- Treat invite tokens, booking codes, exports, auth secrets, and signed URLs as restricted; balances, contacts, receipts, and exact locations as sensitive.
- Deny by default and authorize every resource action from current server-side relationships; JWT claims cannot be the long-term source of membership truth.
- Define API and worker database roles that cannot bypass RLS or mutate ledger tables outside approved procedures.
- Require step-up authentication for owner transfer, full export, sensitive downloads, security changes, and account deletion.
- Define abuse cases for invite enumeration, expense spam, notification harassment, export abuse, and upload bombs before implementation.

## Rollback and Exit Gate

This phase changes documents only. Roll back by superseding an ADR, never by silently editing an approved invariant. Do not begin Phase 2 until all critical ADRs, the permission/data matrices, and the Group/Plan/Participant model are approved; unresolved external-provider choices may remain only if a provider-neutral port and deadline are documented.
