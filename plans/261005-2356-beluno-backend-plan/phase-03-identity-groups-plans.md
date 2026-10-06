---
phase: 3
title: "Identity, Groups and Plans"
status: completed
priority: P1
effort: "4 weeks (2 backend engineers)"
dependencies: [2]
---

# Phase 3: Identity, Groups and Plans

## Context Links

- [Plan overview](./plan.md)
- [Phase 1: Architecture Kernel](./phase-01-architecture-kernel.md)
- [Phase 2: Platform Foundation](./phase-02-platform-foundation.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- OAuth Security BCP: <https://datatracker.ietf.org/doc/html/rfc9700>
- OWASP authorization guidance: <https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html>

## Overview

Implement current identity, device/session metadata, reusable groups, memberships, generic plans, stable plan participants, trip extensions, invites, guest claims, and centralized authorization. This phase creates the tenancy/access spine every later module must use.

## Requirements

### Functional

- Own identity in the API (ADR 0007): Google/Apple ID-token sign-in, passwordless email OTP/magic link, and invite-scoped guest sessions that upgrade without changing user IDs.
- Support reusable groups, group roles/defaults, membership lifecycle, ownership transfer, leave/remove, and group deletion workflow.
- Support generic plans for casual and multi-day use cases with `plan_kind` as defaults only.
- Support plan-level RSVP separately from participant access/role.
- Support optional recurring plan series that materialize independent plan occurrences with timezone-aware exception handling.
- Support plan duplication/reuse through an allowlisted copy manifest that excludes financial history and sensitive/private data.
- Snapshot selected people as stable `PlanParticipant` records; preserve history after group changes, removal, or guest claim.
- Support hashed expiring/revocable/limited-use plan invites, optional approval/email binding, guest/placeholder identities, and atomic claim/merge.
- Support optional one-to-one travel details and travel segments without adding travel requirements to core plans.
- Expose REST resources and delta-visible changes for profiles, groups, memberships, plans, participants, invites, and travel extensions.

### Non-functional

- Check current membership/participant state on every request; do not trust long-lived role claims in JWTs.
- Scope every child query by owning `group_id` or `plan_id`; use composite FKs to prevent cross-tenant associations.
- Owner transfer and invite redemption must be transactional and concurrency safe.
- Removed/left users lose reads and signed-URL issuance immediately while historical attribution remains intact.
- Use step-up authentication for ownership transfer, full export, destructive deletion, and other approved sensitive actions.
- Return indistinguishable/timing-safe invite and resource errors where disclosure would enable enumeration.

## Architecture

### Core entity relationships

```text
auth.users ──1:1── iam.users
iam.users ──< iam.devices

groups.groups ──< groups.group_memberships >── iam.users
groups.groups ──< plans.plans
plans.plan_series ──< plans.plans
plans.plans ──< plans.plan_participants
plans.plan_participants ── optional link ── iam.users
plans.plans ──< plans.plan_invites
plans.plans ──0:1── plans.travel_plan_details ──< plans.travel_segments
```

Suggested key fields:

- `groups`: `id`, `name`, `owner_user_id`, default currency/timezone, `version`, lifecycle timestamps.
- `group_memberships`: `(group_id,user_id)`, role, state, joined/left/removed timestamps, version.
- `plan_series`: `id`, optional `group_id`, title/default snapshot, validated recurrence rule, IANA timezone, generation horizon, lifecycle/version metadata.
- `plans`: `id`, `group_id`, optional `series_id`, occurrence key, title, kind, lifecycle state, date/time range/timezone, base currency, visibility, `version`, `deleted_at`.
- `plan_participants`: `id`, `plan_id`, identity kind, optional `user_id`, display snapshot, role/access state, RSVP response, stable sort key, join/leave/responded metadata, `version`.
- `travel_plan_details`: `plan_id`, destination summary, travel-specific preferences only.
- `travel_segments`: `plan_id`, type, origin/destination, local/UTC times and IANA zones, order key.
- `plan_invites`: `id`, `plan_id`, token hash, role, intended-email hash (optional), max/used count, expiry, state, creator.

### Authorization policy

All controllers call a central policy port with a normalized decision context:

```text
authorize(actor, action, resource, {
  currentGroupMembership,
  currentPlanParticipant,
  role,
  relationship,
  visibility,
  resourceState,
  planState,
  stepUpAge
})
```

Policy defaults deny. Object visibility (shared/private/owner-only), object ownership, participant state, and plan state can narrow role permissions. RLS repeats tenant/current-membership constraints as defense in depth; API policy remains authoritative for business actions.

### Invite redemption and guest claim

Invite tokens are 32 random bytes encoded base64url; only a keyed or strong hash is stored. Redemption transaction locks the invite, validates state/expiry/use/email/approval, resolves or creates exactly one participant, increments use count, creates audit/change records, and returns the canonical result. Guest claim locks guest identity/participant rows, verifies the claimant, links the existing participants to the user where policy permits, resolves duplicates through an explicit audited merge decision, and never rewrites participant foreign keys.

## Proposed File Inventory

All entries are `[UNVERIFIED]` proposed paths.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/src/beluno/modules/iam/*` `[UNVERIFIED]` | User/device/session application model | Auth integration |
| Create | `backend/src/beluno/modules/groups/*` `[UNVERIFIED]` | Groups and membership lifecycle | Policy/concurrency |
| Create | `backend/src/beluno/modules/plans/*` `[UNVERIFIED]` | Plans, participants, invites, series, travel extension | Contract/e2e |
| Create | `backend/src/beluno/modules/plans/policies/*` `[UNVERIFIED]` | Central authorization decisions | Permission matrix |
| Create | `backend/src/beluno/contracts/{iam,groups,plans}.py` `[UNVERIFIED]` | Pydantic request/response/error schemas | OpenAPI |
| Create | `backend/src/beluno/db/models/{iam,groups,plans}.py` `[UNVERIFIED]` | SQLAlchemy mappings | Type/integration |
| Create | `backend/alembic/versions/000010_identity_groups_plans.py` `[UNVERIFIED]` | Tables, constraints, indexes, RLS SQL | Migration/security |
| Create | `backend/tests/security/test_permission_matrix.py` `[UNVERIFIED]` | Every role/state/action | Security gate |
| Create | `backend/tests/integration/test_invite_redemption.py` `[UNVERIFIED]` | Token and concurrency behavior | Integration |
| Create | `backend/tests/integration/test_guest_claim.py` `[UNVERIFIED]` | Stable identity/merge | Integration |
| Create | `backend/tests/e2e/test_group_plan_lifecycle.py` `[UNVERIFIED]` | Primary access spine | E2E |

No existing files are deleted. Phase 2 OpenAPI/module registration files will be modified when concrete paths are verified.

## Implementation Steps

1. Add application-user bootstrap on first valid auth token. Store provider subject, public profile, locale/timezone, status, and lifecycle metadata; never store passwords or refresh tokens.
2. Register device/session metadata and remote-revocation hooks. The API issues and validates its own access tokens; session revocation and current access are checked server-side on every request.
3. Create group tables, application services, policy rules, owner invariants, and REST resources. Implement versioned updates and soft deletion/grace workflow.
4. Create membership lifecycle commands: invite/add, accept, role change, leave, remove, restore if allowed, and atomic owner transfer.
5. Create generic plan tables and commands. Validate kind/date/timezone/base-currency rules without requiring destination or travel dates.
6. Create plan-series and occurrence contracts: validated recurrence/timezone, deterministic occurrence key, bounded materialization horizon, one-occurrence/this-and-future exceptions, and idempotent cancellation/reschedule behavior.
7. Create plan-participant snapshot logic from users, group members, guests, or placeholders; enforce `(plan_id,participant_id)` composite uniqueness/FKs for downstream use.
8. Implement participant access/role changes and separate RSVP responses. Removed participants remain referentially valid but lose access; declined participants keep only policy-approved access/history.
9. Implement allowlisted plan duplication/reuse with explicit participant/default selection and hard exclusion of ledger, settlement, fund, private media, booking secrets, audit, invite tokens, and stale entitlements.
10. Implement travel extension/details and segments with local time, UTC instant, IANA timezone, and date-only semantics reviewed against DST/red-eye cases.
11. Implement invite create/preview/redeem/revoke/rotate with token hashing, expiry, use count, optional intended recipient, approval queue, and rate-limit signals.
12. Implement guest creation and account claim. Resolve duplicate email/link joins transactionally and emit merge/audit events without changing participant IDs.
13. Implement the central policy decision library and controller guard. Generate tests directly from the approved permission matrix.
14. Add RLS policies and database grants for API/worker roles; use request transaction-local identity where feasible. Explicitly test owner/service-role bypass behavior.
15. Publish domain events plus audit/change records in the same transaction for every accepted mutation; never include raw invite token or sensitive profile values.
16. Add list/detail/update endpoints with cursor pagination, optimistic `If-Match`/entity version, stable error codes, and OpenAPI examples.
17. Add abuse limits and security telemetry for invite preview/redemption, membership changes, guest claim, ownership transfer, series generation, and duplication.
18. Execute lifecycle, concurrency, IDOR, RSVP, recurrence, duplication, revoke, and guest-claim E2E suites on real PostgreSQL.

## Todo

- [x] Auth subjects map idempotently to application users and devices.
- [x] Group create/update/membership/owner-transfer/delete lifecycle is complete.
- [x] Generic plan schema supports each casual and multi-day example.
- [x] RSVP is independent from role/access and emits auditable delta-visible changes.
- [x] Recurring series materializes deterministic, timezone-correct independent plan occurrences and exceptions.
- [x] Plan duplication copies only the approved manifest and never financial/sensitive history.
- [x] PlanParticipant IDs remain stable through leave/remove/claim/merge.
- [x] Optional travel details/segments never become core-plan requirements.
- [x] Invite tokens are hashed, expiring, revocable, limited-use, and rate limited.
- [x] Guest claim is atomic, audited, and duplicate-safe.
- [x] Permission matrix tests cover all roles, states, visibility, ownership, and sensitive actions.
- [x] RLS and API policy both deny cross-group/plan access.
- [x] OpenAPI and delta-visible events cover all identity/group/plan mutations.

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | User changes a `plan_id` in a participant/resource URL | `404`/deny; no cross-plan existence leak |
| Critical | Two admins concurrently redeem final invite use | Exactly one succeeds; count and participant remain correct |
| Critical | Guest claims account already represented in plan | Explicit dedup/merge outcome; no duplicate participant or rewritten history |
| Critical | Last owner attempts to leave/remove self | Rejected until atomic transfer/recovery succeeds |
| Critical | Removed participant reuses still-valid JWT | Current membership check denies all plan data and signed URLs |
| High | Group member joins plan after previous expenses | New stable participant created; no retroactive inclusion in past splits |
| High | Owner changes group defaults | Existing plan snapshots do not silently change |
| High | Movie plan omits travel details | All plan and participant APIs work normally |
| High | Weekly sport series crosses DST; worker retries occurrence creation | Local wall time follows the approved rule; occurrence key prevents duplicate plan |
| High | Organizer cancels one recurring occurrence | Other occurrences and series rule remain unchanged; cancellation is audited/synced |
| High | Completed plan is duplicated for the same group | Selected structure/defaults copy; balances, settlements, private media, secrets and old invite/access data do not |
| High | Participant changes RSVP going → declined while still a member | Response updates without role/access corruption; notifications and counts converge |
| High | Red-eye travel segment crosses DST/timezones | Stored/display inputs preserve intended local times and UTC instants |
| Medium | Invite preview uses invalid versus expired token | Safe generic response/timing; security telemetry records category |
| Medium | Concurrent owner transfers | Exactly one final owner result and auditable transition |

## Success Criteria

- [x] Every protected endpoint uses the central current-state authorization check.
- [x] Generated permission suite covers 100% of matrix cells with deny-by-default behavior.
- [ ] Cross-tenant IDOR tests pass for direct IDs, nested IDs, list filters, export placeholders, and signed URL requests. (Direct/nested IDs, list filters, and SQL-level RLS pass; export and signed-URL endpoints do not exist yet — verify when those modules land.)
- [x] Invite/claim concurrency tests prove at-most-once participant creation and no token leakage.
- [x] Removed users lose access immediately while historical participant references remain intact.
- [x] Generic plan creation needs no travel-only field; travel extension supports multi-segment trips.
- [x] RSVP, recurrence/exception, and duplication test suites pass with deterministic time and privacy boundaries.
- [x] All mutations are versioned, audited, idempotency-ready, and emitted to the Phase 4 change contract.
- [x] Phase 4 can implement sync without redesigning identity or tenancy.

## Risk Assessment

- **Membership versus participant confusion:** downstream developers may reference mutable group membership. Enforce composite participant FKs and architecture tests.
- **Guest-account duplicates:** email invite plus link join can create two identities. Use verified-link rules and an explicit merge transaction with audit.
- **JWT authorization staleness:** role claims persist after removal. Resolve membership from PostgreSQL every request, with only short invalidatable caches if later measured necessary.
- **Invite enumeration/abuse:** previews can disclose social data. Use minimal allowlisted preview, generic errors, rate limits, and optional bot challenge.
- **Owner deadlock/recovery:** lost account can orphan a group. Design verified recovery/admin procedure without weakening normal owner invariants.
- **Series explosion or DST drift:** unbounded materialization and UTC-only recurrence create duplicate/wrong plans. Bound the horizon, use local timezone rules, stable occurrence keys, and idempotent workers.
- **Unsafe plan copy:** generic cloning can leak financial or private history. Use a versioned allowlist and negative tests for every excluded data class.

## Security Considerations

- Use Authorization Code + PKCE in clients, short-lived access tokens, rotating refresh tokens/reuse detection via managed auth, and secure device storage.
- Hash invite tokens and intended emails; never log them or include them in analytics.
- Require recent/step-up auth for owner transfer, destructive deletion, and sensitive export.
- API/worker roles must not be table owners or have `BYPASSRLS`; test privileged roles separately.
- Audit all access-control changes with actor, subject, previous/current state, request ID, and safe metadata.

## Rollback and Exit Gate

Use expand-contract migrations and keep new identity fields nullable/backfilled until validated. Never roll back by deleting participants or rewriting participant IDs; disable new invite/claim entrypoints with kill switches while preserving reads and recovery. Exit requires approved permission results, stable participant proof, invite/claim concurrency safety, immediate revoke behavior, and travel-as-extension validation before sync or domain modules build on this spine.

## Execution Decisions (2026-10-06)

Recorded with the user before implementation; they replace `[UNVERIFIED]` paths above.

- Repository root is the backend (no `backend/` prefix). Code lives in `src/beluno/`, migration `alembic/versions/000002_identity_groups_plans.py`, grants in `sql/platform-runtime-grants.sql`.
- No Supabase (ADR 0007). Sign-in: Google/Apple ID token, email OTP + magic link (SMTP through a Procrastinate job; worker generates the code so no secret is stored or queued in plaintext), guest session minted by invite redemption. Tokens: 15-minute ES256 access JWT with `kid`/JWKS, opaque rotating refresh token with reuse detection and a short lost-response grace window.
- Plan `group_id` is nullable (ad-hoc plans); `visibility='group'` requires a group.
- Roles: group `owner|admin|member`; plan `owner|admin|member|viewer|guest`. Participant access `pending_approval|active|left|removed|merged`; RSVP `invited|going|maybe|declined` stays independent.
- Step-up: session `authenticated_at` ≤ 10 minutes for ownership transfer and deletion.
- Phase 3 writes minimal `sync_audit.audit_events` + `sync_audit.change_log` through one recorder port; Phase 4 adds cursors, idempotency replay, tombstones and outbox dispatch.
- Integration tests use `BELUNO_TEST_ADMIN_DATABASE_URL` (local disposable PostgreSQL) and fall back to Testcontainers in CI.

### Slices

| Slice | Scope |
|---|---|
| S0 Identity | settings, signing keys/JWKS, Google/Apple verification, email challenges + SMTP job, sessions/refresh rotation, guest sessions, rate limits |
| S1 Kernel | migration (tables, composite FKs, partial uniques, RLS helpers/policies, grants), SQLAlchemy models, UUIDv7, policy engine, audit/change recorder, `If-Match`/cursor helpers |
| S2 Groups | CRUD, membership lifecycle, atomic owner transfer, deletion grace |
| S3 Plans | lifecycle, timing, participants, RSVP, travel details/segments |
| S4 Invites | hashed tokens, preview/redeem/revoke/rotate, approval, email binding, claim/merge |
| S5 Series + duplicate | rrule subset, DST-safe materialization, exceptions, split, allowlisted copy |
| S6 Verification | permission matrix, IDOR, concurrency, e2e on real PostgreSQL, OpenAPI, docs |

Out of scope here: idempotency-key replay and sync pull (Phase 4), notification fan-out (Phase 7), signed media URLs, deletion purge jobs (Phase 7), production hosting/provider procurement.

## Completion Notes (2026-10-06)

- Delivered in the repo: migrations `000002_identity_groups_plans` and `000003_tenant_write_guards`; `src/beluno/{auth,authorization,modules/{iam,groups,plans,invitations,sync_audit}}`; 47 OpenAPI paths; ADR 0007; executable permission matrix.
- Verification: 291 tests on real PostgreSQL (unit, contract, integration, security incl. RLS insider/outsider probes, e2e), 93% coverage, ruff/mypy strict clean.
- Review (code-reviewer, PASS_WITH_RISK → fixed): request-ID 500, invitation accept/remove race, RLS insider row moves (write-guard triggers), Sentry body capture, closed-plan invites, removed-placeholder claims, offset series time, invite rotation.
- User decisions after review: explicit Google/Apple account linking (no email auto-link); keep session revocation on retired refresh replacement (clients refresh single-flight); placeholders member/viewer only, guest claimers get the guest role.
- Added beyond the original list: group invite links (needed so new groups can admit people).
- Fixed Phase 2 defects found on a real database: migrator lacked CREATE privileges; Procrastinate bootstrap reused a closed pool; coverage needed greenlet tracing.
- Open for later phases: idempotency replay/sync pull (Phase 4), notification fan-out and deletion purge after grace (Phase 7), proxy `--forwarded-allow-ips` at deploy, MFA/passkeys, staging provisioning.
