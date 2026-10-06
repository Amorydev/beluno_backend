---
title: "Beluno Backend Implementation Plan"
description: "Build Beluno's production-ready backend for reusable groups and plans, from casual meetups to multi-day trips."
status: in-progress
priority: P1
tags: [backend, python, fastapi, postgresql, offline-sync, ledger, security]
blockedBy: []
blocks: []
created: 2026-10-06
---

# Beluno Backend Implementation Plan

## Overview

Build the complete Beluno backend for reusable friend groups and generic plans: dinners, coffee, movies, sports, birthdays, custom gatherings, and trips. `Group` is the durable social container, `Plan` is the collaboration and financial aggregate, and `PlanParticipant` is the stable historical identity referenced by plan records. A trip is an optional `Plan` extension, never the root model. This is one unified release scope; the phases below only express implementation dependencies.

Phases 2–3 are implemented in this repository (its root is the backend; no `backend/` prefix). Paths in later phase files remain proposals. Estimates assume a small senior team: two backend engineers plus part-time QA/DevOps/security support, approximately 22–30 calendar weeks including integration and soak; one backend engineer should budget roughly 36–48 weeks.

## Architecture Decision

- Python, FastAPI/Pydantic, REST/OpenAPI, and a modular monolith split by domain boundary.
- Self-hosted stack, no Supabase (decision 2026-10-06, ADR 0007): the API owns identity (Google/Apple ID-token sign-in, email OTP/magic link, invite guests), issues short-lived ES256 access JWTs plus rotating opaque refresh tokens, and runs on standard PostgreSQL; object storage will be S3-compatible. Supabase references in Phases 1, 2 and 7 are superseded by ADR 0007. Clients never perform domain writes directly. The API is the only mutation boundary, with RLS as defense in depth.
- SQLAlchemy async owns typed application queries; Alembic plus reviewed SQL migrations own constraints, indexes, functions, triggers, RLS, and operational changes.
- Procrastinate runs PostgreSQL-backed jobs; transactional outboxes remain authoritative. OpenTelemetry and Sentry provide correlated traces, metrics, errors, and redacted logs.
- PostgreSQL is cross-device authority. Clients use a durable operation outbox; the server exposes idempotent command push and cursor-based delta pull from an append-only `change_log`.
- The finance module uses immutable revisions and balanced postings; projections are disposable and rebuildable.

## Dependency Graph

```text
01 Architecture Kernel → 02 Platform Foundation → 03 Identity, Groups & Plans
                                                    ↓
                                      04 Reliability & Sync Kernel
                                         ↙                     ↘
                              05 Financial Ledger      06 Planning & Coordination
                                         ↘                     ↙
                                      07 Lifecycle & Integrations
                                                    ↓
                                      08 Security, Quality & Release
```

## Phases

| Phase | Dependency | Estimate | Name | Status |
|---:|---|---:|---|---|
| 1 | — | 2 weeks | [Architecture Kernel](./phase-01-architecture-kernel.md) | in-progress |
| 2 | 1 | 3 weeks | [Platform Foundation](./phase-02-platform-foundation.md) | completed |
| 3 | 2 | 4 weeks | [Identity, Groups and Plans](./phase-03-identity-groups-plans.md) | completed |
| 4 | 3 | 4 weeks | [Reliability and Sync Kernel](./phase-04-reliability-sync-kernel.md) | pending |
| 5 | 4 | 5 weeks | [Financial Ledger](./phase-05-finance-ledger.md) | pending |
| 6 | 4 | 5 weeks, parallel with 5 | [Planning and Coordination](./phase-06-planning-coordination.md) | pending |
| 7 | 5, 6 | 4 weeks | [Lifecycle and Integrations](./phase-07-lifecycle-integrations.md) | pending |
| 8 | 5, 6, 7 | 4–6 weeks incl. soak | [Security, Quality and Release](./phase-08-security-quality-release.md) | pending |

## Global Acceptance Criteria

- Every defined module is integrated and releasable together; no phase is treated as a reduced product release.
- All mutations are authenticated, authorized against current relationships, idempotent, audited, and visible through sync.
- Money uses integer minor units; every committed ledger transaction balances to zero and every projection rebuild matches canonical postings.
- Multi-device fault tests converge after duplicated, dropped, reordered, delayed, and lost-ack traffic; financial conflicts never use blind last-write-wins.
- Cross-group/plan IDOR, invite abuse, upload, deletion, backup/restore, RLS-bypass, and sensitive-data redaction gates pass with no open P0/P1 or critical/high security finding.
- Production-like load at 2× forecast, a verified restore drill, rollback rehearsal, runbooks, dashboards, alerts, and a 14-day soak meet agreed SLO/RPO/RTO.

## Red Team Review

- Self-review accepted three gaps: plan-level RSVP/access separation, recurring-series semantics, and privacy-safe plan duplication.
- Updated Phases 1, 3, 7, and 8 with state models, implementation steps, tests, risks, and release evidence.
- Python migration sweep completed: FastAPI/Pydantic, SQLAlchemy/Alembic/Psycopg, Procrastinate, pytest/Hypothesis/Testcontainers/Schemathesis/Locust; old non-Python stack references 0.
- External reviewer could not complete because its usage quota expired; no review claim is attributed to it.
- Whole-plan sweep: 9 plan files checked; stale scope terms 0; broken local links 0; unresolved contradictions 0.

## Unresolved Assumptions

- Confirm launch regions, data residency, retention/legal requirements, expected peak traffic, supported offline duration (proposed 90 days), and initial API consumers.
- Confirm guest participation/claim rules, whether web participants may create expenses, and the final role/permission matrix.
- Select licensed FX, email, push, malware-scanning, PDF/export, and maps/place metadata providers before their integration gates.
- Select the PostgreSQL hosting (managed or self-run) that meets PITR, connection, RPO/RTO needs, and an S3-compatible object store; object bytes require an independent backup strategy.
- Provide Google/Apple client IDs, the production SMTP relay, and the signing-key/secret management procedure before identity launch.
