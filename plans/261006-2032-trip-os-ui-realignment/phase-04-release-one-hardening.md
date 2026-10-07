---
phase: 4
title: "Release 1 hardening"
status: pending
priority: P1
effort: "1–2 weeks + soak"
dependencies: [1, 2, 3]
---

# Phase 4: Release 1 hardening

## Overview

Make release 1 operable. Release 1 covers trip, hangout, money, offline sync, invites, people and crews, activity, and account deletion. This phase takes the parts of the previous plan's Phase 8 that release 1 needs and defers the rest to later releases.

## Requirements

- **Security:**
  - RLS, IDOR, and role/capability matrix suites cover every release-1 entity (plans, participants, crews, activity, all finance entities).
  - Redaction tests cover logs, traces, and activity payloads.
  - A dependency and secret scan runs in CI.
- **Performance:** production-shaped load for 2× forecast covering:
  - quick expense
  - pull of a 10-day, 6-person trip with ~150 expenses
  - settle up
  - ledger-head contention on one busy plan

  Each must meet an agreed p95 per endpoint.
- **Data safety:**
  - a backup and restore drill, with sync generations bumped per `docs/runbooks/sync-operations.md`
  - a ledger reconciliation run on restored data
  - a migration rehearsal on a copy
- **Operations:**
  - staging environment, dashboards and alerts (ledger drift, lock wait, sync queue age, error rate, job dead letters)
  - deploy/rollback rehearsal, kill switches documented
  - runbooks updated for the release-1 surface
- **Soak:** a staging soak with the Android app. Default 7 days, to be confirmed against the old plan's 14.

## Success Criteria

- [ ] No open P0/P1 or critical/high security finding.
- [ ] Load results within the agreed SLOs, recorded in `docs/runbooks/`.
- [ ] Restore drill and rollback rehearsal completed and timed (RPO/RTO recorded).
- [ ] Dashboards and alerts live in staging; the soak completes without data loss or drift.

## Open decisions (before starting)

- Hosting choice (managed PostgreSQL or self-run) and S3-compatible storage (needed later for media).
- SLO targets, soak length, launch regions, and data residency.
