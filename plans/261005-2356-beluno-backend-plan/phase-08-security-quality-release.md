---
phase: 8
title: "Security, Quality and Release"
status: pending
priority: P1
effort: "4–6 weeks including a 14-day production-like soak (whole backend team + QA/DevOps/security review)"
dependencies: [5, 6, 7]
---

# Phase 8: Security, Quality and Release

## Context Links

- [Plan overview](./plan.md)
- [Phase 4: Reliability and Sync Kernel](./phase-04-reliability-sync-kernel.md)
- [Phase 5: Financial Ledger](./phase-05-finance-ledger.md)
- [Phase 6: Planning and Coordination](./phase-06-planning-coordination.md)
- [Phase 7: Lifecycle and Integrations](./phase-07-lifecycle-integrations.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- PostgreSQL row security: <https://www.postgresql.org/docs/current/ddl-rowsecurity.html>
- OWASP Authorization Cheat Sheet: <https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html>
- OAuth Security Best Current Practice: <https://datatracker.ietf.org/doc/html/rfc9700>
- OpenTelemetry semantic conventions: <https://opentelemetry.io/docs/specs/semconv/general/>

## Overview

Prove the complete backend safe and operable as one release: authorization and RLS isolation, financial and sync correctness, privacy/media controls, performance under production-shaped load, provider and infrastructure failure behavior, observability, backup/restore, migration compatibility, canary and rollback, incident response, and public-launch evidence. This phase is not a feature-cutting exercise. A failed gate delays the unified release or disables a defective path with a safety kill switch while it is repaired.

## Requirements

### Functional

- Execute the full permission matrix and horizontal/vertical IDOR corpus for every resource, nested locator, command, sync surface, signed URL, job/admin/support action, and public share.
- Complete unit, golden, property/state-machine, mutation, integration, contract, concurrency, sync-model, migration, security, E2E, load, soak, and chaos suites.
- Operate dashboards, alerts, reconciliations, DLQs, repair/replay, kill switches, backup/restore, deletion, support, billing, and incident workflows from reviewed runbooks.
- Rehearse expand-contract migrations, previous-binary rollback, client compatibility, canary promotion/abort, data repair, and a full isolated disaster recovery game day.
- Produce signed release evidence covering every global acceptance criterion, known risk, exception, owner, expiry, and launch decision.

### Non-functional

- Release has no open P0/P1 defect, no open critical/high security finding, no unexplained ledger/projection drift, and no acknowledged-write loss or duplicate-value incident.
- Production-shaped load at at least 2× the agreed peak stays inside recorded latency, error, lock, connection, queue, and sync SLOs.
- Database RPO ≤5 minutes/RTO ≤4 hours and media RPO ≤1 hour/RTO ≤24 hours are demonstrated by restore, not inferred from provider status.
- A minimum 14-day production-like soak completes with stable error budgets, no invariant violation, and actionable alerting rather than alert fatigue.
- Deployment and rollback preserve accepted operations, event replay, tombstones, idempotency, financial history, and supported old clients.
- Security and operations evidence uses synthetic/redacted data; production access is least privilege, time bound, and audited.

## Architecture

### Security assurance and authorization proof

- Convert the Phase 1 permission matrix into parameterized tests over actor state, group membership, plan participation, role, resource visibility/owner, plan/resource lifecycle state, and action.
- Generate IDOR tests that substitute every path/body/query/nested ID with an object from another plan, group, user, visibility scope, or deleted/removed state. Responses must not reveal existence through body, status detail, timing, search, sync, export, or signed URL behavior.
- Test application policy and PostgreSQL RLS independently. Production API/worker/support roles must not own tables, have `BYPASSRLS`, change actor context, disable triggers, or invoke unapproved procedures.
- Force RLS where defined and test fail-closed behavior when actor context is missing, malformed, stale, or set outside the transaction. Background tasks use explicit service policies and task-scoped grants.
- Test OAuth/JWT issuer, audience, algorithm, expiry, JWKS rotation, refresh reuse/revoke, device/session revoke, anonymous/guest claim, and step-up authentication paths.
- Threat-model invite enumeration/redemption, guest merge, finance manipulation, upload/parser abuse, recap shares, billing webhook replay, export exfiltration, notification harassment, support tooling, dependency/supply-chain compromise, SSRF/open redirect, and denial of service.
- Run SAST, dependency/license/SBOM, secret, IaC/config, container, and DAST scans plus a focused manual review. Findings need severity, owner, evidence, fix/retest, and an expiring exception process; critical/high exceptions do not pass launch.

### Verification pyramid and deterministic fault harness

| Suite | Primary proof |
|---|---|
| Unit/golden | Money/time/permission/provider parsing and stable error behavior |
| Property/state-machine | Ledger zero-sum, revisions/reversals, funds, splits, FX, lifecycle invariants |
| Mutation | Tests catch sign, rounding, authorization and dedup regressions |
| Real-PostgreSQL integration | Constraints, RLS, locks, transactions, idempotency, jobs, migrations |
| Contract | OpenAPI/event/sync/provider/webhook compatibility and generated clients |
| Model-based sync | Multi-device convergence under drop/duplicate/reorder/lost acknowledgement |
| E2E | Complete group → RSVP/series plan → decide → organize → spend → settle → recap/export/reuse flow |
| Load/soak | Capacity, contention, leaks, queue growth, provider backpressure |
| Chaos/DR | Safe degradation, recovery, replay, restore, rollback and runbooks |

Tests use seeded generators, controllable clocks, transaction barriers, provider fakes, network fault proxies, and named fault points. Arbitrary sleeps and flaky timing assertions are prohibited.

### Load, stress, and chaos envelope

- Baseline agreed traffic by endpoint, concurrent active plans, participant fan-out, sync batch/page size, upload bytes, notification volume, and job concurrency; retain the test profile with the release evidence.
- Exercise a 100-participant/10,000-expense plan, high-contention expense writes, burst invite/poll/reminder traffic, large cursor pulls, projection rebuild, export/recap generation, and media scanning while core traffic continues.
- Validate pool/worker budgets, ledger-head wait, deadlocks/serialization retry, plan/scope hot spots, index efficiency, payload limits, storage/egress, queue age, and graceful load shedding.
- Inject API kill after DB commit before response; worker kill after provider acceptance before acknowledgement; duplicate/reordered events/pages; DB failover/connection exhaustion; storage/scanner/FX/email/push/billing/analytics outage; clock skew/DST; corrupted projections; partial object delete; and inconsistent DB/object restore points.
- Preserve finance/auth/sync correctness under degradation. Throttle exports/media/rebuilds before core writes; offer explicit financial read-only kill switch only when integrity cannot be guaranteed.

### Observability and operational controls

- Correlate API → PostgreSQL → job → provider with request, trace, operation, event, job, release, schema, and pseudonymous actor/plan identifiers. Trace all errors and financial mutations within cost limits; sample ordinary reads.
- Dashboards cover API SLO/error budget, DB connections/locks/replica/PITR state, ledger invariant/reconciliation, idempotency replay/collision, sync lag/conflicts/full resync, worker/DLQ age, notification suppression/delivery, media quarantine, export/recap, billing drift, permission denies/abuse, backups, restores, and deployment health.
- Every P0/P1 signal has a threshold, owner, page destination, dedup/silence rule, linked runbook, and synthetic test signal. Alerts must distinguish provider degradation from internal data-integrity failure.
- Audit storage is separate from operational logs, append-only/tamper-evident within chosen controls, access reviewed, and excluded from broad support/search/analytics views.

### Backup, restore, and disaster recovery

- Enable managed PostgreSQL PITR with at least the approved window; take encrypted logical copies into a separate account/region with the approved daily/weekly/monthly retention.
- Enable object versioning/replication or independent media backup and create a daily manifest of storage key, checksum, owner/link, state, and deletion marker. Database backups containing only Storage metadata are insufficient.
- Back up/reproduce migrations, IaC, Auth configuration, RLS/grants, Procrastinate schema/config, schedules, templates, feature/kill-switch state, provider config references, and KMS/key recovery procedures.
- Restore monthly into an isolated environment and run schema checks, row/manifests counts, ledger reconciliation, projection rebuild, RLS/IDOR suite, object checksum sample, application smoke/E2E, and deletion-ledger replay to prevent resurrection of erased data.
- Run a quarterly full game day with declared incident time, actual RPO/RTO, communication log, missing dependency findings, and corrective actions. Production is never overwritten during a drill.

### Migration, canary, and rollback strategy

```text
expand schema/policies → deploy backward-compatible code → bounded backfill
→ validate/reconcile → canary traffic/jobs → promote → adoption window → contract
```

- CI statically rejects or requires explicit rehearsal for table rewrites, unbounded backfills, missing indexes, privilege/RLS changes, unsafe enum edits, destructive drops, and migrations incompatible with the previous binary/client protocol.
- Migrations run once under advisory lock with owner credentials; application roles cannot migrate. Backfills are checkpointed, rate limited, resumable, and observable.
- Canary API instances receive a controlled cohort; canary workers receive only named safe job types/queues until handlers prove compatible. Compare latency, errors, denies, ledger/sync outcomes, event lag, and provider effects against control.
- Rollback re-deploys the prior artifact only while expanded schema/contracts remain compatible. Database/data defects use forward repair migrations or append-only finance corrections; never delete postings, accepted operations, audit, idempotency, or change history as deployment rollback.
- Feature flags are typed, audited, owner/expiry controlled, fail safely, and used for rollout/kill switches only. Removal is scheduled after compatibility and soak gates.

### Launch gates and runbooks

Required runbooks include deploy/rollback; migration/backfill; auth/JWKS/session revoke; suspected IDOR/data leak; invite abuse; ledger invariant/drift and financial read-only mode; sync backlog/full resync; worker/DLQ/replay; provider outages; malware/media quarantine; export/share revoke; billing reconciliation; backup/restore; deletion/privacy request; secret/key rotation; incident severity/communications; and support break-glass.

Launch evidence is organized into six gates inherited from the research findings:

1. Architecture/invariants and threat model approved.
2. Financial truth and exact projection rebuild proven.
3. Sync convergence, offline retention, revocation, and client compatibility proven.
4. Security/privacy/media and all permission/IDOR paths proven.
5. Operations/DR, provider degradation, migration/canary/rollback, and runbooks proven.
6. Unified product E2E, 2× capacity test, 14-day soak, support readiness, and zero blocking findings proven.

## Proposed File Inventory

All paths are proposed and `[UNVERIFIED]` until the backend repository exists.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/tests/security/test_permission_matrix.py` `[UNVERIFIED]` | Exhaustive policy combinations | Security gate |
| Create | `backend/tests/security/test_idor_corpus.py` `[UNVERIFIED]` | Cross-scope identifier substitution | Security gate |
| Create | `backend/tests/security/test_rls_roles.py` `[UNVERIFIED]` | API/worker/support/owner bypass proof | Database security |
| Create | `backend/tests/property/test_system_state_machine.py` `[UNVERIFIED]` | Hypothesis cross-module invariants | Property/soak |
| Create | `backend/tests/sync/test_chaos_model.py` `[UNVERIFIED]` | Faulted multi-device convergence | Sync gate |
| Create | `backend/tests/migration/test_compatibility.py` `[UNVERIFIED]` | Expand/contract and old-binary/client window | Migration gate |
| Create | `backend/tests/e2e/test_unified_product_lifecycle.py` `[UNVERIFIED]` | Complete product acceptance flow | Release gate |
| Create | `backend/tests/load/{api,sync,finance,jobs,media}_locust.py` `[UNVERIFIED]` | Locust capacity/contended traffic profiles | Load gate |
| Create | `backend/tests/chaos/fault_scenarios.py` `[UNVERIFIED]` | Deterministic infrastructure/provider faults | Chaos gate |
| Create | `backend/ops/dashboards/*` `[UNVERIFIED]` | SLO/invariant/security/DR dashboards | Operations |
| Create | `backend/ops/alerts/*` `[UNVERIFIED]` | Actionable alert rules/runbook links | Operations |
| Create | `backend/ops/canary/*` `[UNVERIFIED]` | Promotion/abort checks | Deployment |
| Create | `backend/ops/restore/validate_restore.py` `[UNVERIFIED]` | Isolated restore reconciliation | DR gate |
| Create | `backend/docs/runbooks/{incident,security,ledger,sync,jobs,providers,media,billing,privacy,backup-restore,migration,canary-rollback}.md` `[UNVERIFIED]` | Operator procedures | Game day |
| Create | `backend/docs/release/launch-evidence.md` `[UNVERIFIED]` | Gate sign-off and exceptions | Final review |
| Create | `backend/.github/workflows/{security,nightly,load,release}.yml` `[UNVERIFIED]` | Automated assurance and release control | CI/CD |

Existing Phase 2–7 CI, migrations, telemetry, policy, workers, module contracts, provider adapters, and tests will be modified only after their actual paths are verified. Nothing is deleted as part of planning.

## Implementation Steps

1. Freeze production capacity, SLO/error-budget, RPO/RTO, offline/retention, supported-client, security severity, and launch acceptance thresholds with named owners.
2. Generate the executable permission matrix and IDOR substitution corpus across REST, sync, search, exports, media, recap shares, billing, jobs, admin/support, and signed URLs.
3. Test application authorization and every database role/RLS policy independently, including missing actor context, table-owner/BYPASSRLS attempts, service/background duties, removed users, and private resources.
4. Complete authentication/session, invite/guest-claim, webhook, upload/parser, export/share, notification, rate-limit/abuse, audit, and break-glass threat scenarios; remediate and retest findings.
5. Run SAST, SCA/license/SBOM, secret, IaC/config, container, and DAST pipelines plus manual reviews of finance, sync, RLS, crypto/key use, uploads, billing, and operator tools.
6. Finish unit/golden/property/state-machine/mutation suites. Run fixed seeds on every change and an overnight retained-seed corpus for finance, sync, permission, and lifecycle transitions.
7. Finish real-PostgreSQL integration/concurrency suites, provider contract fixtures, generated-client compatibility, and complete unified-scope E2E journeys.
8. Build production-shaped synthetic data and execute endpoint, hot-plan, finance, sync, worker, notification, media, export/recap, billing, and projection-rebuild load profiles at baseline and 2× forecast.
9. Inject process, network, database, queue, clock, storage, scanner, FX, communication, billing, and analytics failures. Verify safe degradation, bounded retries, replay/dedup, kill switches, and recovery.
10. Finalize telemetry redaction, SLO/invariant/security/DR dashboards, alert thresholds/routes, synthetic probes, cost controls, and on-call ownership; fire every alert in staging.
11. Provision and validate PITR, independent logical/database copies, object backup/versioning/manifests, key/config recovery, deletion-ledger replay, and isolated restore automation.
12. Run an isolated restore drill and full game day; reconcile ledger/projections, test RLS/IDOR, verify objects, measure actual RPO/RTO, and remediate all blocking gaps.
13. Rehearse additive and policy migrations, resumable backfill, old-binary/client behavior, canary API/worker promotion, abort, previous-artifact rollback, and forward data repair.
14. Finalize and tabletop every required runbook with the real dashboards, tools, credentials, approval paths, and communication channels; record operator evidence and gaps.
15. Run the 14-day production-like soak using the release candidate, realistic scheduled jobs/providers/fault probes, daily invariant/reconciliation review, and no uncontrolled schema/config drift.
16. Assemble Gate A–F evidence, close all P0/P1 and critical/high findings, document lower-severity risk acceptance with expiry, obtain accountable sign-off, and perform the canary launch/promotion procedure.

## Todo

- [ ] Capacity, SLO, RPO/RTO, retention, client-window, and launch thresholds are signed off.
- [ ] Permission matrix, IDOR corpus, RLS/role suite, and authentication/session suites pass completely.
- [ ] Finance property/mutation/concurrency and exact projection-rebuild gates pass.
- [ ] Multi-device sync chaos, 90-day offline, tombstone/full-resync, revocation, and upgrade suites pass.
- [ ] Media, notification, billing, analytics, export, public-share, admin, and privacy redaction gates pass.
- [ ] Baseline and 2× load meet SLOs without invariant, pool, lock, queue, storage, or cost failure.
- [ ] Chaos/provider outage tests demonstrate documented safe degradation and recovery.
- [ ] Dashboards, alerts, kill switches, DLQ/replay/repair tools, and runbooks are exercised.
- [ ] Isolated restore and full DR game day meet measured RPO/RTO and deletion-replay requirements.
- [ ] Migration, backfill, canary, abort, application rollback, and forward-repair rehearsals pass.
- [ ] Fourteen-day soak completes with no blocking incident or unexplained drift.
- [ ] Gate A–F release evidence has accountable sign-off and no forbidden exception.

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | User substitutes another plan's participant/expense/booking/asset ID in every locator | Uniform deny/not-found policy; no data, timing, search, sync, export, or signed-URL leak |
| Critical | API/worker/support role attempts table-owner, `BYPASSRLS`, trigger-disable, or cross-plan write | Database denies action; security alert/audit identifies role and request safely |
| Critical | API dies after financial commit and client retries from a 90-day-old queue | One canonical transaction; stored response or explicit full-resync/upgrade recovery, no duplicate value |
| Critical | Restore DB and media from different points, including previously deleted account | Reconciliation detects mismatch; deletion ledger reapplies; no deleted data is served |
| Critical | Canary migration introduces wrong ledger/search policy | Automated checks abort promotion; prior binary remains compatible; forward repair preserves history |
| Critical | Projection is corrupted during settlement traffic | Alert fires; suspect projection is not trusted; shadow rebuild matches canonical ledger before swap |
| High | 100 participants, 10k expenses, concurrent sync/export/media at 2× peak | Core SLO/invariants hold; background work throttles and queues remain within documented limits |
| High | Storage/scanner/FX/push/email/billing providers fail simultaneously | Core plan/finance writes remain correct; each dependent feature enters explicit degraded state |
| High | Worker/provider acknowledgement is lost repeatedly | Delivery/webhook/event dedup and reconciliation prevent unintended repeated business effect |
| High | JWT key rotates while sessions/devices are revoked | Valid rotation continues; revoked/reused tokens fail promptly without stale membership authority |
| High | Malicious upload, webhook replay, recap token enumeration, and export flood | Validation, signatures, entropy, rate limits, quotas, and alerts contain abuse without broad lockout |
| Medium | DST/clock skew during reminders, itinerary, FX and sync ordering | Server sequence/UTC/IANA rules remain deterministic; no duplicate reminder or history rewrite |
| Medium | Recurring sports series crosses DST, one occurrence is skipped, then plan reused | Occurrences stay unique/time-correct; exception and reuse do not leak prior finance/private content |
| Medium | Alert exporter or analytics provider is unavailable during incident | Local safety metrics/logs/runbooks remain usable; bounded buffering avoids memory/queue collapse |

## Success Criteria

- [ ] One reproducible release-candidate artifact passes all mandatory CI, nightly, security, migration, E2E, load, chaos, restore, and soak suites.
- [ ] One hundred percent of planned policy actions and all cross-scope ID substitutions are tested; production runtime roles prove no owner/`BYPASSRLS` path.
- [ ] Ledger/property/reconciliation evidence shows zero invariant violations and byte/logically equivalent projection rebuilds on production-shaped data.
- [ ] Sync fault corpus shows no lost acknowledged writes, duplicate financial value, blind financial LWW, or unresolved convergence failure.
- [ ] P95/error/queue/lock targets pass at 2× forecast and final measured limits are recorded in capacity/runbook documents.
- [ ] Database and media restores meet measured RPO/RTO, pass application/RLS/ledger/object checks, and reapply deletion history.
- [ ] Canary abort and prior-application rollback succeed across a backward-compatible migration; destructive data rollback is unnecessary.
- [ ] Every P0/P1 alert reaches an owner with a correct runbook, and every required operator workflow is exercised in staging/game day.
- [ ] Fourteen-day soak completes with zero data-loss, duplicate-ledger, sensitive-data leakage, or unexplained drift incident.
- [ ] No open P0/P1 defect or critical/high security issue remains; Gate A–F evidence is signed for the entire unified scope.

## Risk Assessment

- **False confidence from coverage:** high percentages can miss state combinations. Gate on invariants, mutation score, model/property tests, IDOR generation, and production-shaped scenarios rather than line coverage alone.
- **Non-representative load:** average traffic hides hot plans and fan-out. Preserve explicit worst-case fixtures and test concurrent background pressure.
- **Unsafe migration rollback:** schema contraction can strand old binaries. Require expand-contract, compatibility probes, canary abort, and forward repair.
- **Restore theatre:** provider backup success does not prove recovery. Measure isolated restore, object checks, deletion replay, ledger reconciliation, and real operator time.
- **Alert fatigue or telemetry leakage:** noisy observability can both fail operators and expose data. Use actionable SLO/invariant alerts, redaction markers, sampling, ownership, and cost limits.
- **Full-scope schedule pressure:** pressure may encourage silent gate waivers. Delay launch or disable a defective path with an explicit safety switch; never weaken financial, security, privacy, sync, or recovery gates.

## Security Considerations

- Deny by default and check permission on every request and delivery; neither UUID entropy, possession of a cursor/job/share ID, nor a valid historical JWT grants resource access.
- Test RLS under actual API, worker, scheduler, support, migration, restore, and emergency roles. PostgreSQL owners/superusers/`BYPASSRLS` identities are prohibited from normal runtime.
- Production security testing uses synthetic fixtures and approved environments; destructive DAST/chaos/load requires explicit scope and must not target user data or third-party providers without authorization.
- Secrets, KMS keys, signing keys, billing/webhook credentials, backup credentials, and break-glass accounts have separate ownership, rotation, recovery, and audit procedures.
- Release artifacts are pinned, provenance/SBOM recorded, dependency vulnerabilities triaged, CI permissions minimized, and production deploy approvals protected.
- Incident and breach runbooks cover evidence preservation, access revocation, containment, user/regulator communication requirements, and post-incident corrective action.

## Rollback and Exit Gate

Before launch, retain the previous signed application artifact, backward-compatible schema generation, tested kill-switch configuration, canary abort automation, database/media restore points, and a reviewed forward-repair procedure. A rollback never truncates `change_log`, idempotency, tombstones, audit, outboxes, billing inbox, ledger transactions, or postings. Financial corrections remain append-only; deletion history is replayed after restore.

Promotion proceeds from staging evidence to a small canary, then controlled expansion only while automated and human gates stay green. Abort on invariant violation, cross-plan disclosure, acknowledged-write loss, duplicate financial value, unbounded queue/lock growth, failed reconciliation, or material SLO regression. The plan is complete only after the unified release passes Gate A–F, canary promotion, 14-day soak, restore/rollback rehearsal, runbook ownership, and all global acceptance criteria; otherwise it remains pending rather than being reclassified as a smaller release.
