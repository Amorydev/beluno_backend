# ADR 0010: Release 1 Operations

**Status:** Accepted

**Date:** 2026-10-07

**Related:** ADR 0005 (security and privacy), ADR 0006 (operations and recovery), ADR 0009 (activity feed and account deletion)

## Context

Release 1 (trip, hangout, money, offline sync, invites, people and crews, activity, account deletion) needs to be operable before a host is chosen. Plans scheduled for deletion, including those a deleted account owned alone, were never removed. Isolation was tested for plans, participants, and invites only.

## Decision

- **Hosting-neutral artifacts.** One container image runs the API, the worker, the scheduler, or the migrate step. A compose stack in `deploy/staging/` runs it on any host. Each process holds only its own database credentials (`BELUNO_PROCESS_ROLE`); only the API and the worker also need the signing keys, the token hash key, and SMTP. The staging environment and a 7-day soak with the Android app follow once a host and region are chosen.
- **Monitoring.** OTel Collector → Prometheus, Alertmanager, and Grafana. Alert rules (with promtool tests) and the dashboard live in the repo. Latency histograms carry bucket bounds at every target.
- **Targets (p95).** Error rate below 0.5 % at about 50 rps peak (2× a forecast of ~5k DAU). `docs/runbooks/performance.md` records the results.

  | Operation | p95 target |
  |---|---|
  | Quick expense | 300 ms |
  | Pull of a 10-day, 6-person trip with ~150 expenses | 1 s |
  | Settle up | 300 ms |
  | 20 concurrent writers on one plan | 1 s |

- **Plan purge.** A plan whose deletion was scheduled is deleted outright `BELUNO_PLAN_PURGE_AFTER_DAYS` (30) later, finance history included. The plan can be restored until then.
  - A SECURITY DEFINER gate purges one plan per transaction, and refuses cutoffs under seven days.
  - Finance history stays append-only for every runtime role. The gate, as the table owner, is the only path that deletes it.
  - Audit events stay; they hold IDs, never text. Each purge adds a `plan.purged` audit event.
  - Only the owner can schedule or cancel deletion, and a schedule is stamped with the current time. A database guard enforces both, so nobody can backdate a plan into the purge.
  - Crews started from a purged plan forget it.
- **Leftover personal data.** Sessions are purged 30 days after they end, so device labels go with them. A deleted crew keeps neither name nor members.
- **Isolation.** Security tests sweep every RLS table, found from the catalog, and every route with path IDs, taken from the live OpenAPI document. They cover cross-tenant reads, writes, and IDOR.
  - Credential tables (sessions, refresh tokens, identities, email challenges, rate-limit windows) stay open to the API role by design: sign-in and refresh reach them before an actor exists. The API filters them by user.
  - Spans and security logs are tested to carry no tokens, emails, names, descriptions, or notes.
- **Supply chain.** CI audits locked runtime dependencies (`pip-audit`), scans git history for secrets (gitleaks), publishes a CycloneDX SBOM, and builds the image.

## Consequences

- A purged plan cannot be recovered except from a backup taken before the purge. Restore drills note this.
- Restoring a backup brings purged plans back. Re-run the purge job after a restore, before reopening writes.
- Moving pre-auth credential lookups behind definer functions would let RLS close the credential tables too. It is not needed for release 1.
- Stored command responses (kept 180 days for replay) may hold personal data of a deleted account or plan until they expire.
