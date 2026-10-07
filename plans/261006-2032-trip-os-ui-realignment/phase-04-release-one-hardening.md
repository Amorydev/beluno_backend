---
phase: 4
title: "Release 1 hardening"
status: in-progress
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

## Execution Decisions (user, 2026-10-07)

- **Hosting-neutral artifacts now:**
  - one container image for the API, the worker, and the scheduler;
  - a staging-shaped compose stack (Postgres, migrate job, API, worker, scheduler, OTel Collector, Prometheus, Alertmanager, Grafana) that runs on any VPS;
  - backup, restore, and migration-rehearsal scripts.

  Staging is provisioned, and the Android soak is run, once a host is chosen. This phase stays `in-progress` until the soak completes.
- **Monitoring:** OTel Collector → Prometheus + Grafana, with alert rules and the dashboard kept as code in the repo.
- **SLOs (p95):**
  - quick expense ≤ 300 ms
  - pull of a 10-day, 6-person trip with ~150 expenses ≤ 1 s
  - settle up ≤ 300 ms
  - 20 concurrent writers on one plan ≤ 1 s

  Error rate < 0.5 %. Load is 2× forecast, about 50 rps at peak (forecast ~5k DAU). A local Locust baseline comes first; staging numbers follow.
- **Plan purge:** 30 days after deletion is scheduled, a plan is deleted outright: participants, invites, finance, activity, and plan-scope change rows. Audit events stay (IDs only). The plan can be restored until then.
- **Defaults** (not contested):
  - 7-day soak;
  - branch `feat/release-one-hardening` from `main`;
  - deleted crews lose their name at once;
  - revoked sessions, with their device labels, are purged 30 days after revocation;
  - stored command responses keep the existing 180-day purge.

## Design

### Plan purge (migration `000010_plan_purge`)

- `finance.reject_history_change` lets DELETE through only for the table owner (never a guarded runtime role) while the transaction-local flag `beluno.plan_purge` is on. Only the purge gate sets that flag, and it clears it before returning. Updates stay rejected.
- `plans.purge_deleted_plan(p_cutoff)` is a SECURITY DEFINER function granted to `worker_runtime`.
  - It refuses a cutoff newer than now − 7 days.
  - It takes one plan with `deletion_scheduled_at <= p_cutoff` (`FOR UPDATE SKIP LOCKED`), deletes it, and returns its ID, or NULL when none is left.
  - Order:
    1. finance tables by `plan_id`, children first: the deferred `expenses → current revision` FK breaks the expense/revision/commitment cycle;
    2. `activity.events`;
    3. invites and participants: `joined_via_invite_id` is cleared first to break their cycle;
    4. plan-scope change rows and scope heads;
    5. other plans' `duplicated_from_plan_id` references are cleared;
    6. the plan row itself.
- `plans.purge_deleted` is a daily worker job that purges in one transaction per plan, up to a batch. New setting `BELUNO_PLAN_PURGE_AFTER_DAYS` (default 30, at least 7).

### Leftover personal data

- `crews.delete_crew` blanks the crew's name (and its members) in the tombstone.
- The hourly auth purge also removes sessions revoked more than 30 days ago (with their refresh tokens).

### Security

- **RLS sweep:** build a tenant with every release-1 entity (trip with expenses, refunds, settlements, waivers, budgets, commitments, kitty, consolidation, base change; crews; activity; invites). An outsider reads zero rows from **every** RLS table, found from the catalog so new tables are covered automatically. Writes are refused.
- **IDOR sweep:** every route with path IDs, taken from the app's route table, is called with another tenant's IDs. The response must never be 2xx and never contain foreign data. Write routes use a curated valid body per route, and a test fails when a route has none.
- **Redaction:** logs (problem responses, unhandled errors), spans (in-memory exporter: no request bodies, tokens, emails, or descriptions), and activity summaries.
- **CI:** `pip-audit` on the locked dependencies, a gitleaks secret scan over git history, a CycloneDX SBOM artifact, `docker build` of the image, and `promtool check rules`.

### Performance

- `tests/load/`: Locust scenarios for quick expense, trip pull (bootstrap plus incremental), settle up, and one-plan contention. A seed script mints sessions directly in a non-production database. Results go to `docs/runbooks/performance.md` against the SLOs.

### Data safety and operations

- `scripts/db_drill.py`: backup (`pg_dump -Fc`), restore into a fresh database, bump sync generations, run ledger reconciliation, and print timings (RPO/RTO inputs). A `rehearse-migration` mode upgrades a restored copy to head and runs the validation queries.
- `Dockerfile` (multi-stage uv, non-root) and `deploy/staging/` compose with OTel, Prometheus rules (ledger drift, ledger lock wait, job queue age, dead letters, error rate, command p95 against the SLOs), Alertmanager, and a provisioned Grafana dashboard.
- Runbooks:
  - deploy/rollback rehearsal on the compose stack;
  - one kill-switch table;
  - the release-1 surface (crews, activity, account deletion, plan purge);
  - SLOs and load results.

## Implementation Steps

1. Plan purge: migration, gate, job, setting, tests (full-data plan purged, a neighbouring plan untouched and reconciling, restore before the cutoff, audit kept).
2. Leftover personal data: crew tombstone and revoked-session purge, with tests.
3. Security sweeps: RLS catalog sweep, IDOR route sweep, redaction tests.
4. Container image, staging compose, observability config, CI jobs.
5. Load harness and a local baseline run.
6. DB drill and migration rehearsal scripts, then a local drill.
7. Runbooks, retention matrix, ADR 0010 (release-1 operations).
8. Review, tests, PR.
9. *(After a host is chosen)* Provision staging, rerun load and the drill there, then soak with the Android app for 7 days.

## Success Criteria

- [x] No open P0/P1 or critical/high security finding (review High fixed; see notes).
- [ ] Load results within the agreed SLOs, recorded in `docs/runbooks/`: local baseline done (all met at ~54 rps); staging after step 9.
- [ ] Restore drill and rollback rehearsal completed and timed (RPO/RTO recorded): local drill done; rollback rehearsal and staging timings after step 9.
- [ ] Dashboards and alerts live in staging; the soak completes without data loss or drift (step 9).
- [x] Plans scheduled for deletion are purged after 30 days with nothing left behind but audit IDs.

## Open decisions

- Host and region (data residency), and S3-compatible storage for later media.

## Progress Notes (2026-10-07)

Steps 1–8 are done on `feat/release-one-hardening`. Step 9 waits for a host.

Gates: ruff, format, and mypy clean; the full suite on real PostgreSQL green (0 skipped), coverage 96 %.

What landed:

- **Retention:** migration `000010_retention_purges`:
  - the plan purge gate and its daily job, with a `plan.purged` audit event;
  - an owner-only deletion schedule stamped with the current time (a DB guard);
  - a session purge 30 days after a session ends;
  - deleted crews blanked, including crews deleted earlier;
  - `plan_id` indexes for the purge.
- **Security sweeps:**
  - RLS over every catalog table: an outsider sees and changes nothing. Credential tables are open to the API by design and listed.
  - IDOR over every OpenAPI route with IDs: only 403/404, and the victim's rows stay unchanged.
  - Telemetry and log redaction, plus Sentry event redaction.
- **Operations:**
  - one image, a staging compose stack (OTel, Prometheus rules with promtool tests, Alertmanager, Grafana);
  - CI: pip-audit, gitleaks, SBOM, image build, config checks, format check;
  - latency buckets at the SLO bounds;
  - `BELUNO_PROCESS_ROLE`, so each process holds only its own credentials.
- **Load:** Locust scenarios and a seed script. Local baseline: every SLO met at ~54 rps with 0 errors. Quick expense goes over 300 ms at ~100 rps.
- **Drills:** `scripts/db_drill.py`:
  - backup → restore → bump → reconcile, about 5–10 s on a 254 MB database;
  - migration rehearsal on a copy;
  - the procedure is in `docs/runbooks/database-drills.md`.
- **Docs:** ADR 0010, `deploy-rollback.md` (rollback rehearsal steps, one kill-switch table, the release-1 job list), the retention matrix, and the README.

Review (`reports/code-reviewer-261007-release-one-hardening-review-report.md`):

- **Fixed:**
  - H1: an admin could backdate a deletion into the purge.
  - M1: missing indexes.
  - M2: migrate and the scheduler needed keys.
  - M4: no purge audit.
  - L1: crews deleted earlier kept their names.
  - L2: crews kept a link to the purged plan.
  - L3–L6: docs and comments; monitoring ports now bind separately.
  - L9: CI format check.
- **Open:**
  - M3: the forwarded-IP default must be checked on the real host (documented).
  - L7: sync push bodies, `sync_audit`, and `iam.users` writes are outside the sweeps.
  - L8: the session purge scans `iam.sessions`.

Reports:

- `reports/ops-artifacts-261007-container-staging-ci-report.md`
- `reports/load-drills-261007-load-baseline-and-db-drills-report.md`
- `reports/code-reviewer-261007-release-one-hardening-review-report.md`

Still open:

- Host and region, and with them RPO/RTO targets and the backup cadence (PITR).
- The soak mode: the stack runs `development` until there is a TLS database and an https collector.
- Profiling quick-expense latency on staging.
