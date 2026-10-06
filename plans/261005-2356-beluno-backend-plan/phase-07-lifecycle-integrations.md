---
phase: 7
title: "Lifecycle and Integrations"
status: pending
priority: P1
effort: "4 weeks (2 backend engineers + fractional QA/product/security review)"
dependencies: [5, 6]
---

# Phase 7: Lifecycle and Integrations

## Context Links

- [Plan overview](./plan.md)
- [Phase 5: Financial Ledger](./phase-05-finance-ledger.md)
- [Phase 6: Planning and Coordination](./phase-06-planning-coordination.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- OWASP File Upload Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html>
- Supabase database backup/storage caveat: <https://supabase.com/docs/guides/platform/backups>

## Overview

Complete the plan lifecycle around the finance and planning cores: transactional notifications, secure media and memories, generated recaps, permission-filtered search and exports, provider-independent billing entitlements, privacy-safe analytics, and audited operations/support tooling. This phase also closes the cross-module workflows that turn separate records into one coherent plan without allowing asynchronous integrations to weaken authoritative transactions.

All modules remain part of the same unified release scope. A feature flag may provide a kill switch or controlled rollout, but it must not disguise an unfinished module or create a smaller product tier for launch readiness.

## Requirements

### Functional

- Translate committed domain events into transactional, actionable, reminder, digest, and separately consented marketing notifications across push and email.
- Accept uploads only through a quarantine-to-ready pipeline; attach ready assets to expenses, bookings, tasks, memories, and recaps with plan- and visibility-aware access.
- Create private memories and immutable recap snapshots from an explicit safe-field allowlist; optionally publish and revoke a recap share without exposing the underlying plan.
- Provide permission-filtered search across safe plan content, and asynchronous exports with manifest, checksum, expiry, deletion, and step-up authorization.
- Reconcile App Store, Play Store, and web billing through provider adapters into one server-authoritative entitlement model; never trust client purchase state alone.
- Emit privacy-safe product analytics from authoritative server outcomes, and provide least-privilege admin/support views and actions with full audit history.
- Complete poll-to-schedule, booking-to-commitment/expense, schedule/place-to-expense, task/reminder, memory/recap, and plan-completion integrations without cross-module repository access.
- Materialize recurring plan occurrences through idempotent scheduled jobs, deliver RSVP/reminder changes, and complete privacy-safe plan duplication/reuse workflows.

### Non-functional

- Domain write plus event/outbox record must commit atomically; provider calls occur after commit and are safe under duplicate delivery.
- Every delivery, download, export, admin action, webhook, and recap share is authorized from current server state at use time, not only when queued or created.
- Restricted/sensitive content never appears in lock-screen notifications, search documents, analytics properties, job dashboards, logs, public previews, or support exports.
- Media bytes and metadata have separate backup/deletion controls; database backup alone is not treated as media backup.
- Provider outage must degrade to queued, quarantined, stale, or temporarily unavailable behavior while preserving the core plan/ledger write.
- Search, recap, analytics, exports, and notifications may be eventually consistent; finance commitment conversion and permission revocation remain synchronous.

## Architecture

### Domain-event and integration boundary

Each accepted command in Phases 3–6 emits a versioned event/outbox record in the same PostgreSQL transaction. Phase 7 consumers use a registry rather than reaching into another module's tables:

```text
domain transaction
  ├── canonical state + audit/change_log
  └── integration_event(event_id, plan_id, type, schema_version, safe payload)
          │
          ├── notification intent/delivery
          ├── search projection
          ├── analytics outcome
          ├── recap invalidation
          └── external-provider job
```

- Event payloads contain safe identifiers and minimum routing facts; consumers query exported read ports after re-authorization when more detail is needed.
- Each consumer records `(consumer_name, event_id, handler_version)` so replay is idempotent and observable.
- Cross-module actions requiring one invariant use synchronous application ports in the originating transaction. Example: converting a booking commitment to an expense is one finance-owned transaction, not two eventual jobs.
- Derived actions such as search indexing, notification delivery, recap invalidation, export generation, and analytics forwarding remain asynchronous.

### Notifications and preference resolution

- Build `notification_intents`, `notification_deliveries`, device tokens, channel preferences, quiet hours, digest membership, suppression reasons, and dead-letter/replay metadata.
- Deduplicate on `(domain_event_id, recipient_id, channel, template_version)`. A worker re-checks active participation, object visibility, plan state, channel consent, quiet hours, and token status immediately before provider delivery.
- Classify notifications as `security`, `transactional`, `actionable_reminder`, `digest`, or `marketing`. Security and required transactional messages do not reuse marketing consent; marketing is separately opted in.
- Lock-screen copy is minimal by default and excludes amounts, exact balance, confirmation codes, hotel/location, private captions, notes, contacts, and signed URLs.
- Scheduled reminders use the plan/item IANA timezone and deterministic occurrence keys. Editing/cancelling the source invalidates or replaces prior jobs.
- Persist attempted, accepted, bounced/invalid-token, suppressed, opened, and actioned states when providers support them. Provider acceptance is not called user delivery.

### Media, memories, and recap

```text
initiated → uploaded → quarantined → scanning → ready
                                      └──────→ rejected
ready → deleting → deleted
```

- The API issues a short-lived, size/MIME-constrained upload URL to a random quarantine key. Finalization verifies owner, checksum, declared length, and object metadata.
- Scanner verifies extension allowlist, MIME and magic bytes; rejects polyglots/active content according to policy; enforces byte/page/pixel/decompression limits; re-encodes supported images; strips EXIF/GPS by default.
- Identity/passport/government-document uploads are rejected until a separate approved compliance workflow exists.
- Only `ready` assets may be attached, downloaded, included in an export, selected as a memory, or used in a recap. Download URLs are short-lived and issued only after current authorization.
- Asset ownership and visibility use plan-scoped links; private packing/memory content remains private even when linked to a shared plan object.
- Recap generation reads an allowlisted projection, pins included object/asset versions, and stores an immutable snapshot. A public recap requires explicit publish, an opaque revocable share token, safe preview fields, and no route to private plan APIs.
- Deletion workflow removes originals, derivatives, exports, CDN copies, links, and share tokens and records checksum/manifest evidence. Object versioning/backup is managed independently of PostgreSQL.

### Search and export

- Maintain a derived PostgreSQL full-text/trigram search document containing only authorized, searchable fields. Booking codes, encrypted notes, receipt OCR, exact balances, contacts, private items, tokens, and deleted content are never indexed.
- Search query first resolves visible plan scopes from current relationships, then filters by visibility/owner and plan state. Search result IDs never grant subsequent access.
- Export is an asynchronous job created after step-up authentication for sensitive/full exports. The worker snapshots an authorization/filter manifest, generates machine-readable JSON/CSV and human-readable summaries as defined by contract, scans output, stores checksum, and returns a short-lived download grant.
- Exports have explicit expiry and deletion; revoked membership prevents new download URLs even if generation completed. Account/plan deletion invalidates and removes outstanding exports.

### Billing and entitlements

- Store provider-neutral products, purchases/subscriptions, provider transactions, entitlement grants, grant source, effective/expiry/revoked times, and reconciliation state.
- Webhook inbox verifies signature and timestamp, stores raw bytes encrypted/restricted, deduplicates provider event ID, tolerates out-of-order delivery, and records every transition.
- Server-side entitlement evaluation combines current grants with plan/user context and returns capability decisions. Clients may display cached state but cannot authorize paid capability from a local receipt.
- Restore purchase, grace period, refund, cancellation, chargeback, duplicate provider account, and account-link/merge flows are explicit. A billing outage must not corrupt plan/ledger data; use a documented grace policy and reconcile later.
- Entitlements limit commercial capabilities/quotas only. They never hide a participant's balances, block required settlement/export/privacy rights, or weaken authorization.

### Analytics, admin, and support

- Server events distinguish `attempted`, `accepted/committed`, `synced`, `provider_accepted`, and `user_actioned`; financial analytics originate from committed server outcomes.
- Use pseudonymous actor/plan identifiers, coarse amount/size buckets only when approved, and a versioned analytics allowlist. Never send descriptions, notes, exact amounts/balances, booking codes, contacts, receipt/OCR content, exact locations, invite tokens, or signed URLs.
- Consent/region rules are evaluated before forwarding. A local audit count may exist even when third-party analytics forwarding is disabled.
- Admin/support is a separate protected route surface and database role. Tools expose safe read models, sync/job/export diagnostics, entitlement state, and audited repair/replay commands—not unrestricted SQL or silent impersonation.
- Break-glass access requires step-up authentication, reason, expiry, least-privilege grant, alert, and immutable audit. Support bundles are generated from an allowlist and default to hashes/counters rather than payloads.

### Safe degradation and lifecycle completion

- Notification outage queues intents and surfaces lag; the originating action remains committed.
- Scanner outage leaves assets quarantined. Storage outage blocks only media/export operations.
- Search/analytics lag does not block core writes and is repaired by replaying integration events.
- Billing provider uncertainty applies the approved grace/restricted policy without deleting entitlements or user content.
- Plan completion freezes a recap input watermark, reviews unresolved money/tasks/bookings, schedules reminders, and creates recap/export work only after authoritative modules report a consistent state.

## Proposed File Inventory

All paths are proposed and `[UNVERIFIED]` until the backend repository exists.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/src/beluno/modules/engagement/{notifications,preferences,digests}/*` `[UNVERIFIED]` | Notification intent, policy, templates, delivery | Worker/integration |
| Create | `backend/src/beluno/modules/media_memories/{uploads,assets,memories,recaps}/*` `[UNVERIFIED]` | Quarantine, visibility, memories, recap snapshots | Security/E2E |
| Create | `backend/src/beluno/modules/search_export/{search,exports}/*` `[UNVERIFIED]` | Safe search projection and export workflow | Integration/load |
| Create | `backend/src/beluno/modules/billing/{entitlements,webhooks,providers}/*` `[UNVERIFIED]` | Provider-neutral commerce state | Contract/security |
| Create | `backend/src/beluno/modules/analytics_ops/{analytics,admin,support}/*` `[UNVERIFIED]` | Privacy-safe events and audited operations | Redaction/security |
| Create | `backend/src/beluno/contracts/{notifications,media,search_export,billing,analytics_ops}.py` `[UNVERIFIED]` | Pydantic API/event contracts | OpenAPI/compatibility |
| Create | `backend/src/beluno/db/models/{engagement,media,billing,operations}.py` `[UNVERIFIED]` | SQLAlchemy mappings/queries | Type/integration |
| Create | `backend/alembic/versions/000050_lifecycle_integrations.py` `[UNVERIFIED]` | Tables, constraints, grants, RLS SQL | Migration/security |
| Create | `backend/src/beluno/worker/tasks/{notifications,media,search,exports,billing,recaps,analytics}/*` `[UNVERIFIED]` | Idempotent lifecycle workers | Worker/chaos |
| Create | `backend/src/beluno/scheduler/lifecycle.py` `[UNVERIFIED]` | Reminders, cleanup, reconciliation | Clock/schedule |
| Create | `backend/tests/integration/test_lifecycle_integrations.py` `[UNVERIFIED]` | Event-to-effect correctness | Integration |
| Create | `backend/tests/security/test_media_and_export.py` `[UNVERIFIED]` | Upload/download/share/export isolation | Security |
| Create | `backend/tests/e2e/test_plan_lifecycle.py` `[UNVERIFIED]` | Create through recap/reuse flow | E2E |

Phase 2 runtime/provider adapters, Phase 4 outbox/change handlers, and Phase 5–6 public ports will be modified only after their actual paths are verified. Nothing is deleted.

## Implementation Steps

1. Freeze versioned integration-event schemas, consumer ownership, replay policy, sensitive-field allowlist, and synchronous-versus-asynchronous cross-module map.
2. Implement the integration consumer registry, idempotent consumer checkpoints, replay tooling, lag/DLQ metrics, and provider adapter interfaces over Phase 4 jobs/outboxes.
3. Implement notification intent creation, recipient resolution, current-state re-authorization, preference/consent/quiet-hour policy, deduplication, template versioning, provider delivery, invalid-token handling, and digest grouping.
4. Add deterministic reminder scheduling/cancellation for polls, schedule items, tasks, bookings, settlement readiness, and plan lifecycle using IANA timezone fixtures.
5. Add asset/upload/link tables, storage policies, plan-scoped RLS, quarantine signed-upload/finalize endpoints, quotas, checksums, orphan cleanup, and private download issuance.
6. Implement malware/content scanning, MIME/magic-byte validation, limits, image re-encode, EXIF removal, ready/rejected transitions, derivative creation, and deletion manifests.
7. Implement private/shared memories, selection rules, recap input watermark, allowlisted immutable snapshot, generation worker, explicit publish/revoke, and public-share view.
8. Build safe search documents, event-driven updates/deletes, authorization-filtered queries, reindex/reconciliation jobs, and leakage regression fixtures.
9. Implement asynchronous export requests, step-up authorization, snapshot manifest, format generators, checksum/scan, expiring download, deletion, and privacy-portability coverage.
10. Implement billing provider adapters, signed webhook inbox, receipt/transaction reconciliation, entitlement projection, grace/refund/chargeback/restore/account-link handling, and audit.
11. Implement a central entitlement decision port and enforce it at capability/quota boundaries without blocking safety, balance visibility, settlement, export, or deletion rights.
12. Define the analytics taxonomy and allowlist; instrument authoritative server outcomes, consent/region filtering, pseudonymization, delivery retry, and schema validation.
13. Implement admin/support policies, safe diagnostic views, time-bound break-glass, replay/repair commands, redacted support bundles, and complete operator audit.
14. Complete and contract-test poll-to-schedule, booking-to-commitment/expense, schedule/place-to-expense, task/reminder, media-to-memory, completion-to-recap, recurring-series materialization, RSVP delivery, and allowlisted plan-reuse workflows.
15. Exercise provider outages, duplicate/out-of-order webhooks/events, revoked users, deleted sources, stale jobs, and replay from an empty derived projection.
16. Publish OpenAPI/events, operational dashboards, reconciliation reports, provider-degradation instructions, and data-lifecycle runbooks needed by Phase 8.

## Todo

- [ ] Every cross-module flow has one authoritative owner and transactional/event boundary.
- [ ] Notification intents/deliveries re-authorize, deduplicate, respect consent/quiet hours, and redact sensitive copy.
- [ ] Media cannot leave quarantine until all validation/scan/transform gates pass.
- [ ] Private assets, memories, exports, and recap shares enforce current visibility and revocation.
- [ ] Search and recap projections contain only allowlisted fields and rebuild from events/source ports.
- [ ] Export generation, expiry, deletion, and step-up authentication are complete.
- [ ] Billing webhooks and purchase restoration converge to one audited entitlement state.
- [ ] Entitlements never obstruct balances, settlement, portability, or deletion rights.
- [ ] Analytics schemas and support/admin views pass sensitive-data redaction tests.
- [ ] Provider outage, DLQ, replay, reconciliation, and lifecycle cleanup tooling is operational.

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | Participant removed after notification queued but before send | Delivery is suppressed after current-state authorization; no sensitive preview leaks |
| Critical | Polyglot/decompression-bomb upload with forged MIME | Object remains quarantined then rejected; no derivative/download/search link exists |
| Critical | Private memory selected while public recap is generated | Private content is excluded unless explicitly permitted by safe publish policy |
| Critical | Export completes after requester loses plan access | No download grant is issued; artifact expires/deletes and event is audited |
| Critical | Duplicate/out-of-order refund and purchase webhooks | Inbox deduplicates/reconciles; entitlement converges without duplicate grant |
| Critical | Recurrence materializer retries across DST and scheduler failover | Stable occurrence key creates each plan once with approved local-time semantics |
| Critical | Plan reuse job encounters private memory and booking secret | Copy manifest excludes both; audit records exclusions without copying sensitive values |
| High | Booking converts to expense while search/analytics workers are down | Finance commitment converts exactly once; derived consumers catch up by replay |
| High | Push provider accepts then worker dies before acknowledgement | Retry/dedup records one intended delivery; duplicate risk is measured and bounded |
| High | Notification body test marker contains exact balance/code/location | Template/redaction validation blocks delivery and raises a safe diagnostic |
| High | Media scanner/storage/search/billing provider outage | Core plan/finance writes remain correct; affected feature enters documented degraded state |
| High | Owner revokes a published recap while CDN caches exist | Token and origin access fail immediately; CDN invalidation completes and is tracked |
| Medium | DST transition moves a reminder across local midnight | One reminder follows source timezone/rule; no duplicate or skipped occurrence |
| Medium | Rebuild search, recap candidates, and analytics counts from events | Derived outputs match current authorized sources and exclude deleted/private records |

## Success Criteria

- [ ] Event replay from a clean derived state reproduces notifications intents, search documents, recap invalidations, analytics counts, and entitlement projections without duplicate effects.
- [ ] Recurring occurrence, RSVP notification, and plan-reuse workflows are idempotent, timezone-correct, and privacy-safe.
- [ ] Notification delivery tests prove current authorization, preference/consent, quiet-hour, timezone, deduplication, and sensitive-copy rules.
- [ ] Upload security tests prove quarantine, file limits, scan/transform, ready-only access, short-lived download, backup manifest, and complete deletion.
- [ ] Search/export/recap red-team fixtures find no restricted/private field in indexes, files, previews, analytics, logs, or public shares.
- [ ] Billing reconciliation passes duplicate, delayed, out-of-order, refund, chargeback, restore, grace, and account-link scenarios.
- [ ] All named cross-module workflows produce one authoritative state transition and no finance/budget double count.
- [ ] Admin/support actions are least privilege, step-up protected where required, reasoned, expiring, and immutably audited.
- [ ] Dashboards expose consumer/outbox age, delivery suppression/failure, quarantine backlog, export age, webhook drift, entitlement reconciliation, and derived-projection lag.
- [ ] Phase 8 receives complete lifecycle runbooks, provider-degradation switches, security fixtures, and production-shaped load data.

## Risk Assessment

- **Sensitive-data fan-out:** notifications, search, analytics and exports multiply leakage paths. Use one data-classification allowlist, re-authorize at consumption, and test markers across every sink.
- **At-least-once duplicates:** provider side effects can repeat after lost acknowledgement. Use deterministic delivery keys, provider idempotency where available, reconciliation, and bounded user-visible copy.
- **Media abuse/cost:** large or malicious uploads can exhaust storage/CPU. Enforce pre-upload quotas, hard limits, quarantine, bounded scanners, lifecycle deletion, and separate rate limits.
- **Billing disagreement:** client, store and webhook views can diverge. Treat provider-verified server projection as authority and expose reconciliation/grace states.
- **Cross-module cycles:** convenience joins can collapse boundaries. Use public ports/events, contract tests, and dependency linting; keep invariant-critical transitions synchronous under one owner.

## Security Considerations

- All object IDs, search results, job IDs, event IDs, export IDs, purchase IDs, and share tokens are untrusted locators; authorize the underlying relationship/action on every use.
- Store invite/share/download tokens hashed where replay lookup permits; use random storage keys, short-lived signed URLs, encrypted restricted webhook/export payloads, and key-rotation procedures.
- Provider webhooks require raw-body signature verification, timestamp/replay windows, IP signals only as defense in depth, and a permanent inbox audit.
- Runtime workers receive narrow task-specific grants; replay/admin tooling cannot run under a removed actor's former authority.
- Public recap endpoints expose only immutable allowlisted snapshots and resist token enumeration, cache leakage, indexing, and open redirect behavior.
- Data export and break-glass support require step-up authentication, rate limits, anomaly alerts, and operator/reason audit.

## Rollback and Exit Gate

Deploy integrations additively: persist events/intents before enabling consumers, shadow-build search/entitlement/recap projections, then enable delivery/provider adapters per channel with kill switches. On defect, stop the affected consumer or public share/download issuance; do not roll back authoritative plan/finance state or delete outbox evidence. Schema corrections use forward-compatible migrations, and old event handlers remain available for the supported replay window.

Exit requires every integration consumer to be idempotent/replayable, media and export security gates to pass, entitlement reconciliation to converge, all cross-module flows to avoid duplicate state/value, current authorization to be enforced at delivery/download, lifecycle dashboards/runbooks to operate, and documented degraded behavior to preserve the complete unified product for Phase 8 hardening.
