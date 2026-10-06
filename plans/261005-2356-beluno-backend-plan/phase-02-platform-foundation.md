---
phase: 2
title: "Platform Foundation"
status: completed
priority: P1
effort: "3 weeks (2 backend engineers + fractional DevOps)"
dependencies: [1]
---

# Phase 2: Platform Foundation

## Context Links

- [Plan overview](./plan.md)
- [Phase 1: Architecture Kernel](./phase-01-architecture-kernel.md)
- [High-risk backend findings](./research/high-risk-backend-findings.md)
- Supabase database backups caveat: <https://supabase.com/docs/guides/platform/backups>
- OpenTelemetry semantic conventions: <https://opentelemetry.io/docs/specs/semconv/general/>
- FastAPI application structure: <https://fastapi.tiangolo.com/tutorial/bigger-applications/>
- SQLAlchemy asyncio: <https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html>
- Alembic migrations: <https://alembic.sqlalchemy.org/en/latest/>
- Procrastinate task queue: <https://procrastinate.readthedocs.io/en/stable/>

## Overview

Create the backend repository, runtime roles, typed configuration, managed-service connectivity, database ownership model, migration discipline, CI gates, and observability foundation. The same versioned build must run as API, worker, or scheduler, and every environment must be reproducible without giving clients direct domain-write privileges.

## Requirements

### Functional

- Scaffold a Python workspace with FastAPI/Pydantic API, Procrastinate worker, and scheduler entrypoints.
- Connect to Supabase Auth, PostgreSQL, and Storage using distinct least-privilege identities.
- Establish PostgreSQL schemas per module and a reviewed Alembic/SQL migration workflow compatible with SQLAlchemy models/queries.
- Add request authentication, request context, correlation IDs, normalized errors, health/readiness endpoints, graceful shutdown, and basic admin diagnostics.
- Generate and compatibility-check the REST OpenAPI document and typed clients.
- Provide local/test environments with real PostgreSQL and storage/provider fakes; seed only synthetic data.
- Use SQLAlchemy async with Psycopg and one explicit `AsyncSession` per request/job transaction; no shared session and no implicit lazy I/O.

### Non-functional

- Pin Python and uv/toolchain versions and produce reproducible locked environments/builds.
- Enforce lint, typecheck, unit, integration, migration, OpenAPI, dependency, and secret-scan gates in CI.
- Standardize on pytest/pytest-asyncio, Hypothesis, Testcontainers, Schemathesis, and Locust for deterministic unit/property/integration/API/load coverage.
- Use separate database roles for migrations, API, worker, scheduler, read-only support, and restore drills.
- Centralize configuration validation; startup fails closed on missing/invalid security-critical configuration.
- Redact secrets, authorization headers, tokens, signed URLs, finance descriptions, booking codes, contacts, and exact locations from telemetry.
- Support zero/low-downtime expand-contract migrations and old-binary compatibility for at least the defined rollback window.

## Architecture

### Proposed workspace

```text
backend/
├── pyproject.toml
├── uv.lock
├── src/beluno/
│   ├── api/          # FastAPI routers/dependencies/error handlers
│   ├── worker/       # Procrastinate app and task registry
│   ├── scheduler/    # periodic enqueue only; no domain mutation bypass
│   ├── modules/      # domain modules
│   ├── db/           # SQLAlchemy models/queries/transactions
│   ├── contracts/    # Pydantic DTOs, errors, event/sync envelopes
│   ├── sync/         # protocol/kernel shared code
│   ├── observability/# OTEL, Sentry, logger, redaction
│   └── testkit/      # fixtures, clocks, provider fakes, DB harness
├── alembic/          # reviewed migrations and raw SQL helpers
├── sql/              # reviewed functions, triggers, RLS and validation SQL
├── openapi/
├── tests/{unit,property,contract,integration,sync,security,e2e,load}/
└── docs/{architecture,adr,runbooks}/
```

### Runtime and database roles

- `migrator`: DDL owner; never used by application traffic.
- `api_runtime`: execute/select on approved surfaces; no table ownership, `BYPASSRLS`, unrestricted storage, or auth-admin privilege.
- `worker_runtime`: execute job handlers and narrowly scoped data procedures; not a superuser.
- `scheduler_runtime`: enqueue named jobs only.
- `support_readonly`: time-bound audited access to explicitly safe views.
- `restore_validator`: isolated-environment role for drill checks.

Supabase service credentials are server-only. Mobile/web use Supabase Auth to obtain identity tokens, then call this API. Storage uploads use API-authorized short-lived signed URLs and quarantine keys.

### Migration discipline

1. SQLAlchemy models and typed query/service code do not replace reviewed PostgreSQL SQL.
2. Alembic revision plus any raw SQL includes forward action, lock/scan risk, validation query, compatibility statement, and rollback/roll-forward note.
3. Use expand → deploy compatible code → backfill in bounded worker jobs → validate → contract after adoption.
4. Run migrations once per deploy with an advisory lock; do not run from every API instance.
5. Reject destructive/locking migrations in CI unless explicitly waived with rehearsal evidence.

### Request pipeline

```text
edge limits → request ID → JWT verification → actor/device context
→ input validation → current authorization policy → transaction/application service
→ audit/change/outbox in same commit → response envelope → redacted telemetry
```

External calls are not made while holding core database transactions. Domain state and outbox commit first; workers perform provider calls with idempotent delivery.

## Proposed File Inventory

All entries are `[UNVERIFIED]` proposed paths.

| Action | Proposed path | Purpose | Test impact |
|---|---|---|---|
| Create | `backend/pyproject.toml` `[UNVERIFIED]` | Package metadata, dependencies and tool config | CI smoke |
| Create | `backend/uv.lock` `[UNVERIFIED]` | Reproducible dependency lock | Install gate |
| Create | `backend/src/beluno/api/main.py` `[UNVERIFIED]` | FastAPI bootstrap/lifespan | API smoke |
| Create | `backend/src/beluno/worker/main.py` `[UNVERIFIED]` | Procrastinate worker bootstrap | Job smoke |
| Create | `backend/src/beluno/scheduler/main.py` `[UNVERIFIED]` | Scheduler bootstrap | Schedule smoke |
| Create | `backend/src/beluno/config.py` `[UNVERIFIED]` | Pydantic Settings validation | Config tests |
| Create | `backend/src/beluno/db/session.py` `[UNVERIFIED]` | Async engine/session/transaction context | Integration |
| Create | `backend/src/beluno/db/roles.py` `[UNVERIFIED]` | Role/session-local tenant context | Security |
| Create | `backend/src/beluno/contracts/errors.py` `[UNVERIFIED]` | Stable RFC 9457 error envelope | Contract |
| Create | `backend/src/beluno/observability/*` `[UNVERIFIED]` | OTEL/Sentry/log redaction | Redaction tests |
| Create | `backend/src/beluno/testkit/*` `[UNVERIFIED]` | DB clock/provider fixtures | All suites |
| Create | `backend/alembic/versions/000001_platform.py` `[UNVERIFIED]` | Schemas, roles, extensions, reviewed SQL | Migration test |
| Create | `backend/openapi/openapi.yaml` `[UNVERIFIED]` | Generated public contract | Diff/lint |
| Create | `backend/docker-compose.test.yml` `[UNVERIFIED]` | Real PostgreSQL integration harness | Integration |
| Create | `backend/.github/workflows/backend-ci.yml` `[UNVERIFIED]` | Quality/security gates | CI |
| Create | `backend/docs/runbooks/deploy-rollback.md` `[UNVERIFIED]` | Safe deployment procedure | Rehearsal |

No existing files are modified or deleted because no codebase exists.

## Implementation Steps

1. Pin a supported stable Python release and uv version; initialize Ruff formatting/linting, mypy strict checks, import-boundary rules, and pytest projects.
2. Scaffold the three runtime entrypoints from shared packages; implement signal handling, connection draining, and version/build metadata.
3. Add typed configuration with environment separation and secret references; prohibit secret values in checked-in configuration or logs.
4. Provision development/staging Supabase projects or equivalent isolated environments. Document project tier, region, PITR, connection, storage, and egress limits.
5. Create database schemas and roles; revoke `public` defaults, table ownership, direct client access, and broad Storage permissions.
6. Configure SQLAlchemy async engines with Psycopg separately for API and worker; budget pool sizes against provider limits, create one AsyncSession per request/job, and fail readiness when exhausted/unhealthy.
7. Add SQLAlchemy async model/query scaffolding and Alembic revisions with reviewed raw SQL for triggers/RLS/functions. Validate fresh install, forward upgrade, old-binary compatibility, and failed-migration recovery; prohibit implicit async lazy loads in request code.
8. Implement JWT verification from Supabase issuer/JWKS, actor/device/request context, and deny-by-default authentication guards. Authorization policy arrives in Phase 3.
9. Implement request/response validation, problem/error envelope, safe body limits, request IDs, structured logging, and global exception mapping.
10. Instrument HTTP, PostgreSQL, jobs, and provider ports with OpenTelemetry; initialize Sentry with release/environment tags and before-send scrubbing.
11. Set up Procrastinate task registration, retry/dead-job conventions, task payload schema versioning, queues/locks, periodic scheduling, and scheduler-only enqueue behavior. Keep the transactional application outbox as the canonical domain-event boundary.
12. Add health (`process alive`) and readiness (`dependencies usable`) checks without exposing sensitive topology/version details publicly.
13. Generate OpenAPI from decorated contracts, lint it, diff breaking changes, and generate a disposable client in CI as proof.
14. Create pytest/pytest-asyncio + Testcontainers real-Postgres harness, Hypothesis strategies/state machines, Schemathesis OpenAPI checks, deterministic clock/ID helpers, fake auth/storage/provider adapters, Locust profiles, and synthetic seed builders.
15. Add CI gates and artifact/SBOM/dependency scans; protect production deployment on successful migrations, smoke checks, and explicit approval.
16. Draft deploy, rollback, migration, secrets rotation, and local setup runbooks; rehearse a staging rollback with an additive schema change.

## Todo

- [x] Repository and three runtime roles build from one locked workspace.
- [ ] Ruff, mypy, pytest, Hypothesis, Testcontainers, Schemathesis, and Locust run from pinned uv environments.
- [x] API uses FastAPI/Pydantic and publishes validated REST/OpenAPI output.
- [x] ~~Supabase Auth/Postgres/Storage environment and access model are documented.~~ Superseded by ADR 0007 (self-hosted identity, standard PostgreSQL).
- [x] Runtime database roles are least privilege and cannot bypass RLS by ownership.
- [x] Alembic revisions, SQLAlchemy models, and reviewed PostgreSQL SQL remain consistent in CI.
- [x] Procrastinate executes a synthetic PostgreSQL-backed job with dedup/retry metadata.
- [ ] OTEL traces correlate API → database → worker; Sentry release tags are present.
- [x] Sensitive-data redaction tests cover logs, traces, exceptions, and job payload displays.
- [ ] Fresh database, forward migration, failed migration, and old-binary rollback rehearsals pass.
- [ ] CI enforces type, test, OpenAPI, migration, dependency, and secret gates.

## Test Scenario Matrix

| Priority | Scenario | Expected result |
|---|---|---|
| Critical | Client tries direct insert with anon/authenticated Supabase role | Denied; domain writes require API role/path |
| Critical | API runtime attempts migration/owner-only action | Denied by database privileges |
| Critical | Missing JWT issuer, DB TLS, or encryption key config | Process fails startup with non-sensitive diagnostic |
| High | Migration applied while previous API build serves traffic | Old build continues supported reads/writes during rollback window |
| High | API receives malformed/oversized JSON | Stable validation/413 response; no expensive parse/job side effect |
| High | Worker dies after handler side effect but before ack | Retry follows handler idempotency contract; observable in DLQ metrics |
| High | Sentry exception includes token/booking code test marker | Marker is removed before transmission |
| Medium | OTEL exporter unavailable | Request proceeds with bounded buffering; no process memory spiral |
| Medium | Database pool exhausted | Readiness fails and API sheds load with retryable response |
| Medium | OpenAPI breaking response field removal | Compatibility check fails CI |

## Success Criteria

- [ ] Clean checkout can install, lint, typecheck, build, test, migrate, and start all runtimes using documented commands.
- [ ] API/worker/scheduler use different least-privilege roles and no runtime owns domain tables.
- [ ] Direct client domain writes and private-object reads fail in integration tests.
- [ ] Every request and job has correlated trace/request/operation/release metadata without sensitive content.
- [ ] OpenAPI is linted, versioned, compatibility-checked, and used to generate a client in CI.
- [ ] Migration rehearsal demonstrates an additive deploy and application rollback without data loss.
- [ ] Staging smoke tests prove Auth, database, transactional job, signed quarantine upload, and telemetry connectivity.
- [ ] Phase 3 can add modules without changing platform topology.

## Risk Assessment

- **Scaffold sprawl:** too many packages slow delivery. Keep packages limited to shared technical kernels and domain modules with actual ownership.
- **Supabase privilege leakage:** service role or table owner can bypass controls. Never expose service credentials; test every runtime role directly.
- **Connection exhaustion:** API plus worker can exceed managed limits. Allocate pools explicitly, use provider-compatible pooling, and alert before saturation.
- **Migration lock incidents:** generated migrations may scan/rewrite large tables. Review SQL, measure locks, and use expand/backfill/contract.
- **Telemetry leakage/cost:** broad automatic instrumentation may capture payloads. Disable body capture, sample reads, trace all errors/financial commands within budget.
- **Task-queue maintenance risk:** Procrastinate's project health may change. Keep a narrow queue port and authoritative application outbox, prove retry/locks/periodic jobs in staging, and retain the option to replace the dispatcher without changing domain modules.

## Security Considerations

- Verify JWT algorithm, issuer, audience, expiry, and key rotation; reject tokens on validation uncertainty.
- Use secret manager/provider configuration, rotation runbooks, and separate environment credentials.
- Enforce TLS to database/providers and encrypt sensitive backups/configuration.
- Add dependency provenance/SBOM, lockfile integrity, secret scanning, and critical-vulnerability response policy.
- Public health endpoints reveal no database names, provider keys, stack traces, or detailed dependency errors.

## Rollback and Exit Gate

Rollback deploys the previous application artifact only while migrations remain backward compatible; schema mistakes are corrected with forward migrations unless a rehearsed safe rollback exists. Exit requires working staging infrastructure, least-privilege proof, CI/OpenAPI/migration gates, telemetry redaction, and a successful deploy/rollback rehearsal. Phase 3 must not begin on an unreviewed production-role model.

## Completion Notes (2026-10-06)

Marked completed on the user's statement. Items still unchecked (OTEL end-to-end correlation, failed-migration and old-binary rollback rehearsals, dependency/secret scanning in CI, staging smoke tests) were not verifiable from this repository and remain release-evidence work for Phase 8. During Phase 3 the migrator privilege grants and the Procrastinate bootstrap connector were fixed after the first run on a real database.
