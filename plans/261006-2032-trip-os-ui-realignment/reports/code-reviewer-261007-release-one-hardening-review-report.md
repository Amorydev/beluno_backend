# Code Review: feat/release-one-hardening

Date: 2026-10-07. Reviewer: code-reviewer (read-only, live PG18 probes).

## Scope

- Commits bc39831, 2e4fefe, 56a4a80, 3d92445 (`git diff origin/main...HEAD`, 35 files, +2819/-137), plus the uncommitted
  docs: README.md, docs/adr/0010, docs/runbooks/deploy-rollback.md, docs/runbooks/sync-operations.md, docs/contracts/retention-matrix.md.
- Excluded: tests/load/, scripts/load_seed.py, scripts/db_drill.py (another agent is still writing them).
- How I checked: I migrated a throwaway DB to head and read the FK graph, indexes, triggers, grants, and policies from the catalog.
  I ran pytest probes in the scratchpad (insider backdating, audit metadata text scan) and probed the OTel histogram labels and views.
  I ran promtool check/test (in Docker), the compose shared-image build, pip-audit, and cyclonedx locally. The probe databases are dropped.

## Gates

| Gate | Result |
|---|---|
| `ruff check .` | pass |
| `ruff format --check .` | pass (276 files) |
| `mypy src` (strict) | pass (138 files) |
| Changed/new tests (purge, telemetry, worker jobs, crews, account deletion, activity, idor, rls, unit config/redaction) | 43 passed, 0 skipped, live PG |
| promtool check rules / test rules (prom v3.15.0) | SUCCESS (10 rules) |
| pip-audit (locked runtime export) | no known vulns |
| cyclonedx SBOM command | generates and validates |

## Overall

The purge SQL is careful. The delete order is correct for every FK at head, including the invite/participant cycle, the
expense current-revision deferred FK, and the self-references. The append-only exception can only be reached by the
owner role. The security sweeps are real and would catch regressions. The main problems are three. First, the new
purge turns an existing insider-writable column into irreversible deletion of finance history. Second, the purge
scans whole tables because several FKs have no index. Third, `ProcessRole` does not deliver what it claims for
migrate/scheduler in secure environments.

## Critical

None.

## High

### H1. An admin-level insider can backdate `deletion_scheduled_at` and get a plan, with its append-only finance history, purged within 24 h
- Where: `alembic/versions/000010_retention_purges.py:80-85` (purge selects on `deletion_scheduled_at <= cutoff`). The guard
  is `plans.plan_write_guard` (latest in `alembic/versions/000008_money_alignment.py:722`). It lets owner OR admin
  update any plan column except id/creator/type/base currency.
- Probe (verified): plan with owner Ann and admin Bea. On an `api_runtime` connection with `app.actor_id = Bea`:
  `UPDATE plans.plans SET deletion_scheduled_at = now() - interval '400 days'` -> 1 row. The next
  `plans.purge_deleted` run purged the plan, and `finance.expense_revisions` left = 0.
- Why it matters: in the API, DELETE is owner-only and needs a step-up, and the 30-day restore window is a user decision.
  Write-guard triggers exist to hold against a compromised API or an injection running in an actor's context. Before this
  branch, a forged `deletion_scheduled_at` was reversible. Now it irreversibly destroys append-only ledgers that other
  members rely on. The 7-day floor in the SQL gate checks the cutoff, not the timestamp the row carries, so it gives no
  protection here.
- Fix (new migration that extends the guard): for guarded runtimes, when `deletion_scheduled_at` changes, require
  `plans.actor_plan_role(id) = 'owner'`. Also require `NEW.deletion_scheduled_at IS NULL OR NEW.deletion_scheduled_at
  BETWEEN transaction_timestamp() - interval '5 minutes' AND transaction_timestamp() + interval '5 minutes'`, or make the
  DB stamp it.
  Add a security test next to `tests/security/test_insider_write_guards.py`.

## Medium

### M1. The purge does full-table scans: several FKs and its own `DELETE ... WHERE plan_id` have no usable index
- Catalog check at head. These tables have no index leading with `plan_id`: `finance.expense_payers`, `expense_splits`, `refund_shares`,
  `consolidation_rates`, `consolidation_lines` (their PKs lead with revision/refund/consolidation id). `activity.events` has
  no `plan_id` index at all (only `(scope_type, scope_id, id)` and `occurred_at`). `plans.plans.duplicated_from_plan_id` is unindexed.
  `EXPLAIN DELETE FROM finance.expense_splits WHERE plan_id = ...` stays a Seq Scan even with `enable_seqscan=off`.
- Cost per purged plan:
  - explicit deletes: about 5 finance seq scans, `activity.events` x1, `plans.plans` x1 (the `duplicated_from` UPDATE);
  - FK checks fired by `DELETE plan_participants` (`WHERE plan_id=$1 AND participant_id=$2`): payers, splits, refund_shares,
    consolidation_lines, each scanned once per participant (about 24 scans for a 6-person trip);
  - FK checks fired by `DELETE plans`: `activity.events` and `plans.plans` once more each.
  - Total: about 30 full scans of the largest high-churn tables per plan, x up to 200 plans per run.
  These are the busiest tables in the system (every expense revision and every mutation's feed event).
- Fix: in a new migration, `CREATE INDEX CONCURRENTLY` on `(plan_id, participant_id)` for payers/splits/refund_shares/consolidation_lines,
  on `activity.events (plan_id)`, and on `plans.plans (duplicated_from_plan_id) WHERE duplicated_from_plan_id IS NOT NULL`.
  Or delete the children via `USING finance.expense_revisions r WHERE r.plan_id = target` and
  `activity.events WHERE scope_type='plan' AND scope_id=target`. The FK checks still need the indexes. Re-check with the load seed.

### M2. `BELUNO_PROCESS_ROLE=migrate|scheduler` still requires the auth signing keys, token hash key, and SMTP in staging/production
- Where: `src/beluno/config.py:200-245`. The role only narrows the DSN list; `BELUNO_AUTH_SIGNING_KEYS`, `BELUNO_TOKEN_HASH_KEY`,
  and the console/SMTP checks apply to every role. `scripts/bootstrap_database.py` calls `get_settings()`, so the check runs at startup.
- Probe (verified): `Settings(environment='staging', process_role='migrate'|'scheduler', <own sslmode=require URL>)` ->
  `RuntimeError: Missing ... BELUNO_AUTH_SIGNING_KEYS, BELUNO_TOKEN_HASH_KEY`.
  `compose.yaml` gives migrate and scheduler only `*beluno-base-environment` (no keys, console email by default). With
  `BELUNO_ENVIRONMENT=staging` both containers fail. The only workaround is handing the ES256 signing keys to the migrator
  container, which defeats the least-privilege goal.
- The unit test `tests/unit/test_config.py::test_each_process_needs_only_its_own_database_url` misses this because
  `secure_settings()` always supplies the keys and SMTP.
- The docs overclaim: `docs/runbooks/deploy-rollback.md:14-15` says "start-up checks ask for nothing else", and ADR 0010 says
  "Each process holds only its own database credentials".
- Fix: scope the auth requirements to API/WORKER (and ALL), and the email requirements to API/WORKER. Test each role with a
  minimal environment.

### M3. With the default `FORWARDED_ALLOW_IPS=127.0.0.1`, a host reverse proxy is not trusted, so all clients may share one rate-limit bucket
- Where: `deploy/staging/compose.yaml:130`, `src/beluno/api/dependencies.py:56` (`client_subject` = `request.client.host`).
- On Linux with Docker's default userland proxy, a host nginx/caddy connecting to the published `127.0.0.1:8000` reaches the container
  from the bridge gateway (172.x.0.1), not 127.0.0.1. Uvicorn then ignores X-Forwarded-For. Every unauthenticated
  caller (OTP challenge, sign-in) gets the gateway address, and the soak testers lock each other out.
  Not reproduced on a Linux host here; verify on the target VPS.
- Fix: pin the compose network subnet (`networks.default.ipam.config`) and default `FORWARDED_ALLOW_IPS` to its gateway.
  Or run the proxy inside the compose network and document it. Add a smoke check that `/v1/...` sees the real client IP.

### M4. No audit record of an irreversible purge, and the change rows are written outside `record_mutation`
- Where: `000010_retention_purges.py:91-103` calls `sync_audit.append_changes` directly. Nothing writes an audit event
  (`plan.purged`, actor = system) for the plan purge or the session purge.
- CLAUDE.md says to extend the `record_mutation` seam rather than write audit/sync rows elsewhere. A SQL gate cannot
  use the Python recorder, but it can insert an `audit_events` row (worker and definer can both insert).
  Without one, forensics after H1-style abuse relies on noticing that the `plan.deletion_scheduled` audit row is less than 30 days old.
- Fix: insert one `audit_events` row per purged plan (`action='plan.purged'`, `entity_id=target`, `plan_id=target`,
  `metadata` holding counts only). Optionally do the same for the session purge count.

## Low

- L1. Crews that were already deleted keep their names. The migration changes the CHECK and `forget_member`, but does not run
  `UPDATE people.crews SET name='', member_user_ids='{}' WHERE deleted_at IS NOT NULL` (`000010:178-183`). The decision is
  "deleted crews lose their name at once". This matters only if a shared DB already has tombstones.
- L2. `people.crews.source_plan_id` (exposed in `contracts/people.py:79`) keeps the ID of a purged plan. The purge clears
  `plans.duplicated_from_plan_id` (`000010:142`) but not this column. It is only an ID, but the handling is inconsistent, and clients may deep-link to a 404.
- L3. The stale comment in `deploy/staging/prometheus/rules/beluno-alerts.yml:8-10` says "SDK default buckets ... interpolated".
  `latency_views()` now sets bounds at 300 and 1000 ms (verified with an InMemoryMetricReader: bounds
  `(5,...,300,500,750,1000,...)` and labels `http.target` = route template, `http.status_code`).
- L4. `BELUNO_PUBLISH_ADDRESS` (`compose.yaml:191,203` and grafana) controls the API and the unauthenticated Prometheus and Alertmanager
  together. An operator who sets it to 0.0.0.0 to expose the API also exposes Alertmanager, where anyone can create silences.
  Use a separate `BELUNO_MONITORING_PUBLISH_ADDRESS` defaulting to 127.0.0.1.
- L5. Docs link to files that do not exist yet: `docs/runbooks/performance.md` and `docs/runbooks/database-drills.md`
  (README.md:116-117, deploy-rollback.md:38,48,57,108, ADR 0010:17). They presumably land with the in-flight load/drill
  work; do not merge the docs without them. deploy-rollback step 5 lists "authorization denials" on the Grafana overview,
  but no such panel exists (only request rate by status).
- L6. `sync_audit.operations` response bodies (titles, descriptions) outlive a purged plan by up to 180 days. This follows the
  user decision and ADR 0010 states it, but `docs/contracts/retention-matrix.md` (new purge row) does not.
- L7. Gaps in the security sweeps (the sweeps themselves are not vacuous; see below):
  - `test_idor_sweep.py` only covers routes with path IDs. `/v1/sync/push` (IDs in the body) and the mixed "own plan +
    victim child ID" attack through push are not swept, despite the docstring's "Every route that takes an ID".
  - `test_rls_isolation.py` sweeps writes only in the plans/finance/people/activity schemas. `sync_audit.*` and `iam.users`
    writes are not covered, and `sync_audit.operations` is classed `NOT_TENANT_DATA` although it holds tenant response bodies.
    Note `audit_events_insert` has `WITH CHECK (true)`, so an insider can forge audit rows for any plan (pre-existing).
  - `test_finance_history_stays_append_only_for_runtime_roles` passes because runtime roles lack the DELETE grant (42501
    from the ACL), not because of the trigger's `is_guarded_runtime()` branch. If someone later grants DELETE, the trigger branch is untested.
- L8. `iam.purge_stale_sessions` runs hourly with an OR predicate across three unindexed timestamp columns, a seq scan of
  `iam.sessions`. Acceptable at release-1 size.
- L9. The CI quality job still has no `uv run ruff format --check .` step (pre-existing; it is a CLAUDE.md gate). Base images are
  pinned by tag, not digest.

## Edge cases checked and found clean

- Delete order vs FKs (all 100 FKs at head): every table that references plans, plan_participants, plan_invites,
  plan_ledger_heads, or the finance parents is emptied first. The invites<->participants cycle is broken by
  nulling `joined_via_invite_id`. The deferred `expenses_current_revision_fkey` is satisfied at commit. Self-FKs
  (`merged_into_participant_id`, `reverses_transaction_id`) are deleted in one statement. The finance table list in the purge
  equals the catalog minus the global `currencies` and `market_rates`.
- Triggers: the deferred `enforce_single_owner` skips missing plans. The write guards return early for migrator (definer).
  `activity.events` append-only covers UPDATE only. No finance trigger other than `reject_history_change` fires on DELETE.
- Append-only exception: only migrator holds DELETE on finance tables (25/25). The flag is transaction-local and cleared.
  `is_guarded_runtime()` uses `current_user`, so the definer context is required. No other function deletes from finance.
- Concurrency: restore and leave both take the plan row `FOR UPDATE`, so the purge's `SKIP LOCKED` serializes with them
  (no deadlock path through scope heads). Finance writes and invite redeem refuse deletion-scheduled plans.
  `finance.reconcile_plan` is one SQL statement (one snapshot) and returns nothing for a vanished plan, so there are no false drift alerts.
  `queueing_lock` plus `SKIP LOCKED` prevents a double purge.
- Sync signal: `plan_access` delete uses `participant.version + 1`, matching `record_participant_change`. It goes to every
  non-merged participant with a user (removed/left included, matching `_page_plan_access`). No jobs carry plan IDs.
- Audit metadata scan after `full_tenant`: only codes/states/currencies/booleans, no free text. "IDs only" holds.
- Session purge: ended sessions are already tombstoned by `_load_session`/`_page_sessions`, so there is no sync impact. Reuse detection
  only loses tokens of sessions that can no longer refresh. Grants: EXECUTE goes to worker only, and PUBLIC is revoked.
- Migration header has forward action, lock risk, validation, compatibility, and rollback. Both new functions use
  `SET search_path = pg_catalog, pg_temp` and REVOKE PUBLIC.
- `delete_crew` and `forget_member` blank names, and the crew guard allows it. Behaviour is otherwise unchanged.
- Security tests are non-vacuous:
  - IDOR: tables and routes are found dynamically, 403/404 are the only accepted refusals, a body is required for every write
    route, there is a mixed-tenant attack, the victim's state is diffed, and `len(refused) >= 60`.
  - RLS: the catalog-driven table list is asserted equal to grown ∪ NOT_TENANT_DATA, and `len(swept) >= 25`.
  - The `CREDENTIAL_TABLES` exemption matches the actual `USING (true)` policies (a pre-existing design that ADR 0010 states).
  - Telemetry: asserts the security events are present and greps spans and logs for 8 secrets. No secret-bearing query or path
    parameters exist in the OpenAPI spec.
- Dockerfile: non-root UID 10001, the app tree is root-owned, read-only rootfs in compose, `cap_drop: ALL`,
  `no-new-privileges`. No secrets are baked in, and `.dockerignore` excludes `.env*`. Postgres and the collector publish no ports.
  Compose builds the shared image tag before dependent services and does not pull it (verified with a probe compose).
- CI: `permissions: contents: read`. The gitleaks binary is pinned with a SHA256 checksum, and the allowlists are narrow (path + exact secret).
  pip-audit and the SBOM commands work locally.
- `alembic/env.py` `disable_existing_loggers=False` is fine. `ProcessRole.ALL` keeps the old behaviour.

## Recommended actions (in order)

1. H1: extend `plan_write_guard` (owner-only change, timestamp pinned to transaction time) and add an insider test.
2. M1: add the indexes (CONCURRENTLY) and/or rewrite the child deletes, then measure a purge on the load seed.
3. M2: scope the auth/email checks by process role, add per-role minimal-env tests, and fix the deploy-rollback/ADR wording.
4. M3: pin the compose subnet and set `FORWARDED_ALLOW_IPS` to it, or document running the proxy inside the network.
5. M4: write a `plan.purged` audit row inside the gate.
6. Lows as convenient: L1 backfill, L3 comment, L4 separate publish address, L5 links, L6 matrix note, L7 push IDOR sweep.

## Plan follow-ups

- Phase-04 artifact items (image, staging stack, alerts as code, purge, security sweeps) are implemented. The phase stays
  in-progress per the Execution Decisions (soak pending). H1 and M2 should be fixed before staging is provisioned.

## Unresolved questions

1. M3 depends on the target host's Docker networking (userland proxy on/off, proxy on the host or in a container). Confirm when the VPS is chosen.
2. Is the soak meant to run with `BELUNO_ENVIRONMENT=staging`? The bundled Postgres has no TLS, so secure mode needs an
   external TLS database and an https collector. As shipped, the stack can only run in `development` mode (console email
   prints OTP codes to the logs).
3. Should admins be able to schedule plan deletion at all at the DB layer, or only owners (as the API policy says)? H1's fix assumes owner-only.

Status: DONE_WITH_CONCERNS
Summary: Gates and 43 changed tests pass, and the purge's FK order, append-only exception, and security sweeps are sound. A verified insider path (admin backdates `deletion_scheduled_at`) turns the new purge into irreversible deletion of finance history, and the purge does full-table scans of the largest tables.
Concerns/Blockers: H1 (insider-triggered purge) and M2 (migrate/scheduler cannot start in secure mode without auth keys) should be fixed before staging; M1 before production data volumes.
