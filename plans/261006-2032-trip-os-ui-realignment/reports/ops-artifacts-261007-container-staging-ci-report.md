# Ops artifacts: container image, staging stack, CI scans

Date: 2026-10-07. Branch `feat/release-one-hardening`. Nothing committed.

## Files created / modified

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage (uv 0.11.7 builder, python:3.12-slim runtime), non-root uid 10001, bytecode compiled, no secrets. Default CMD = API. |
| `.dockerignore` | Context = pyproject, uv.lock, README, alembic.ini, src, alembic, sql, scripts only. |
| `.gitleaks.toml` | Extends defaults; 2 exact-match allowlists (see Secret scan). |
| `.github/workflows/backend-ci.yml` | `quality` untouched (+ top-level `permissions: contents: read`); new jobs `dependency-audit`, `secret-scan`, `sbom`, `container`. |
| `deploy/staging/compose.yaml` | postgres, migrate, api, worker, scheduler (profile), otel-collector, prometheus, alertmanager, grafana. |
| `deploy/staging/env.example` | Placeholder-only env (`CHANGE_ME`). |
| `deploy/staging/postgres/init-roles.sh` | Role bootstrap for the container (copy of `sql/init-local-roles.sh`, DB name parameterised). Mode 755. |
| `deploy/staging/otel-collector/config.yaml` | OTLP gRPC in, prometheus exporter :8889, self-metrics :8888, traces to `debug` sink. |
| `deploy/staging/prometheus/prometheus.yml` | Scrapes collector (+ collector internal, self); loads rules; alertmanager target. |
| `deploy/staging/prometheus/rules/beluno-alerts.yml` | 10 alerts. |
| `deploy/staging/prometheus/tests/beluno-alerts.test.yml` | `promtool test rules` unit tests (12 cases). |
| `deploy/staging/alertmanager/alertmanager.yml` | Placeholder receiver (no delivery). |
| `deploy/staging/grafana/provisioning/{datasources,dashboards}/*.yaml`, `grafana/dashboards/beluno-overview.json` | Provisioned datasource (uid `beluno-prometheus`) + 9-panel dashboard. |

## Image: how the process is selected

```
API (default CMD)  uvicorn beluno.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
worker             python -m beluno.worker.main
scheduler          python -m beluno.scheduler.main
migrate            python scripts/bootstrap_database.py   (alembic upgrade head + jobs schema + grants, migrator role)
```

- `--forwarded-allow-ips` comes from the env var `FORWARDED_ALLOW_IPS` (uvicorn reads it when the flag is absent; default trusts loopback only). Compose passes `${FORWARDED_ALLOW_IPS:-127.0.0.1}`.
- HEALTHCHECK = `GET /health/live` (not `/health/ready`, so a DB blip does not restart healthy API containers). Compose disables it for worker/scheduler/migrate.
- The project is installed editable under `/app` (not site-packages) because `beluno.db.bootstrap` resolves `alembic.ini` and `sql/` relative to the source tree (`parents[3]`). Runtime image keeps the same `/app` layout, root-owned and read-only to the app user. Operator scripts (`scripts/jobs.py`, `scripts/finance.py`) are in the image.

## Running the stack

```bash
cp deploy/staging/env.example deploy/staging/.env        # .env is git-ignored
openssl rand -hex 24                                      # one per *_PASSWORD (URL-safe only)
uv run python scripts/generate_signing_key.py --with-token-hash-key   # paste both lines into .env
$EDITOR deploy/staging/.env                               # replace every CHANGE_ME
docker compose -f deploy/staging/compose.yaml up -d --build
docker compose -f deploy/staging/compose.yaml run --rm scheduler      # optional queue smoke test (one-shot)
docker compose -f deploy/staging/compose.yaml logs migrate            # migration output
```

- Ports (bound to `BELUNO_PUBLISH_ADDRESS`, default 127.0.0.1): API 8000, Prometheus 9090, Alertmanager 9093, Grafana 3000 (user `admin`, password `GRAFANA_ADMIN_PASSWORD`). OTLP 4317 stays internal.
- Order: postgres healthy -> migrate completes -> api, worker.
- Roles are created only on the first start of an empty `postgres-data` volume.
- Each process gets only its own DB URL (api/worker/scheduler/migrate); signing keys and SMTP secrets go to api + worker only.
- Kill switches (`BELUNO_*_ENABLED`, `BELUNO_SYNC_DISABLED_COMMANDS`) and sync limits pass through from `.env`; recreate api and worker after a change (`up -d`).
- Unchanged placeholders fail closed for the signing keys (invalid JSON/PEM) and token hash key (< 32 chars), but NOT for the DB/Grafana passwords (a literal `CHANGE_ME` is a valid password). Operator must replace them.

## Assumed Prometheus metric names

Verified empirically: I ran the real OTel collector contrib 0.162.0 binary with this repo's `config.yaml`, drove the app's actual `configure_observability` + instruments + FastAPI instrumentation (opentelemetry-instrumentation-fastapi 0.66b0) at it over OTLP gRPC, and scraped `:8889/metrics`. Names below are what was observed, not just derived.

| OTel instrument | Prometheus series (labels) |
|---|---|
| `http.server.duration` (ms) | `http_server_duration_milliseconds_{bucket,sum,count}` (`http_method`, `http_status_code` e.g. `"500"`, `http_target` = route template e.g. `/v1/sync/pull`, `/v1/plans/{plan_id}`) |
| `beluno.command.duration` (ms) | `beluno_command_duration_milliseconds_{bucket,sum,count}` (`command`) |
| `beluno.finance.ledger_lock.wait` (ms) | `beluno_finance_ledger_lock_wait_milliseconds_{bucket,sum,count}` |
| `beluno.finance.ledger.drift` | `beluno_finance_ledger_drift_total` (`problem`) |
| `beluno.jobs.queue` gauge | `beluno_jobs_queue` (`measure` = `todo`/`doing`/`failed`/`oldest_waiting_seconds`) |
| `beluno.sync.push.results` | `beluno_sync_push_results_total` (`outcome`) |
| `beluno.sync.pull.pages` | `beluno_sync_pull_pages_total` (`scope_type`, `status`) |

- `resource_to_telemetry_conversion` adds `service_name`, `service_instance_id`, `deployment_environment_name`, ...; the exporter also sets `job` = service name (`beluno-api` / `beluno-worker`) and `instance`. Prometheus scrape uses `honor_labels: true` so these are kept rather than renamed `exported_job`.
- HTTP metrics use the default (old) semconv. If `OTEL_SEMCONV_STABILITY_OPT_IN=http` is ever set, they become `http_server_request_duration_seconds_*` with `http_response_status_code`/`http_route`; rules and dashboard would need updating.
- Command names: quick expense = `expense.create`; settle up = `settlement.record` (there is no `settlement.create`). Sync pull is NOT a command and has no duration metric, so its SLO uses `http_server_duration_milliseconds_bucket{http_method="POST",http_target="/v1/sync/pull"}`.
- Ledger drift and queue gauge are emitted by the worker process; commands/ledger-lock/HTTP by the API.

## Alerts (all in `rules/beluno-alerts.yml`)

LedgerDrift (critical; `increase[15m] > 0` OR series newly appeared, `keep_firing_for: 6h`), LedgerLockWaitHigh (p95 > 1s, 10m), JobQueueOldestWaitingHigh (> 300s, 5m), JobsFailed (> 0, 15m, matches runbook), HighServerErrorRatio (5xx > 0.5%, 10m, critical), QuickExpenseLatencyAboveTarget (p95 > 300ms), SettleUpLatencyAboveTarget (p95 > 300ms), SyncPullLatencyAboveTarget (p95 > 1s), plus two monitoring-path alerts I added: CollectorDown, WorkerQueueMetricsMissing (`absent(beluno_jobs_queue)` 15m; a dead worker would otherwise silence queue and drift alerts).

Why the drift expression has two branches: a counter series is only created at its first increment, and `increase()` cannot see a series' first sample, so a first-ever finding would never alert. The test suite covers both branches.

## Validation done locally (real tools, not just parsing)

- `promtool` 3.15.0: `check config` (incl. rule_files), `check rules` (10 rules), `test rules` SUCCESS (12 test cases incl. first-appearance drift, no-drift, 5xx above/below threshold, per-command latency isolation, pull route selection). All 18 dashboard PromQL expressions also pass `promtool check rules`.
- `otelcol-contrib` 0.162.0 `validate` OK on the collector config, and live-scraped as described above.
- `amtool` 0.29.0 `check-config` OK.
- `docker compose ... config` (CLI works without a daemon): OK with `env.example`; fails with a clear message when `.env` is missing; `--profile scheduler` OK.
- Runtime flow without Docker, on a throwaway PG18 cluster: `init-roles.sh` -> `scripts/bootstrap_database.py` (migrations 1..10 + jobs schema; second run is a no-op) -> API under `api_runtime` returns ok on `/health/live` and `/health/ready` -> `beluno.scheduler.main` prints job id -> worker starts and idles without error. (Cluster stopped; password was throwaway, nothing written to the repo.)
- All YAML/JSON/TOML parse; `sh -n` on the init script; `uv run ruff check .` and `ruff format --check .` pass.
- `pip-audit`, `cyclonedx-py` and the `uv sync --locked --only-group dev` + `--no-sync` command forms used in CI were run locally with the same flags.
- gitleaks 8.30.1 (darwin binary, same version CI pins): full history scan clean with `.gitleaks.toml`.

## pip-audit findings

Locked runtime deps (60 packages, `uv export --no-dev`): **No known vulnerabilities found** (exit 0, `--strict`). All groups (dev included): also none. Nothing upgraded.

## Secret scan

Without config, history scan found 2 false positives of `generic-api-key` (commit c6d77ba): `ec.SECP256R1` in `src/beluno/auth.py:87` and prose `join/leave/responded` in `plans/261005-2356-beluno-backend-plan/phase-03-identity-groups-plans.md:72`. `.gitleaks.toml` allowlists exactly those (rule + path + matched secret text, AND-ed). Negative control: the same auth.py text in another path is still flagged. History scan with config: no leaks. CI installs the pinned release binary with a sha256 check (no licensed Action, no Docker needed).

Local working-tree scans also hit `.kilo/worktrees/dent-sail/...` copies of the same two lines; `.kilo` is untracked/not in history, so CI is unaffected.

## CI jobs

- `dependency-audit`: `uv sync --locked --only-group dev`; `uv export --no-dev --no-emit-project`; `pip-audit -r ... --no-deps --disable-pip --strict`.
- `secret-scan`: fetch-depth 0; gitleaks 8.30.1 binary (checksum pinned); `gitleaks git --redact --config .gitleaks.toml .`.
- `sbom`: `cyclonedx-py requirements` (CycloneDX 1.6 JSON, validated; 60 components) uploaded as artifact `sbom-cyclonedx`.
- `container`: `docker build` (no push); asserts uid 10001, imports every entrypoint, `alembic.ini` + `sql/` present; `docker compose config` with `env.example` (incl. scheduler profile); `promtool check config|rules` and `test rules`, `amtool check-config`, collector `validate`, each run in the same image tag that compose uses (read from `docker compose config --images`, so no duplicated pins).

## Could NOT be validated locally

- `docker build` of the Dockerfile and the container assertions in CI (no Docker daemon). Reviewed by hand; main risks: uv discovering the base image's python (`UV_PYTHON_DOWNLOADS=never`), editable install path layout. CI is the first real build.
- `docker compose up` end to end (healthchecks, `read_only: true` + `tmpfs /tmp` on the app containers, `service_completed_successfully` ordering, Grafana provisioning actually loading the dashboard JSON, Postgres 16 alpine running `init-roles.sh`; I ran the script on PG18 against a plain cluster, it is plain SQL).
- Live scrape of the containerised collector (done with the native binary of the same version instead).
- Linux gitleaks binary checksum was taken from the official release `checksums.txt`, but the linux download itself was not executed.

## Concerns / open questions

1. **Scheduler is not a daemon.** `beluno.scheduler.main` defers one heartbeat job and exits. Periodic jobs (`app.periodic`) are scheduled by the worker process. So the compose `scheduler` service is a one-shot under profile `scheduler` (otherwise `up --wait` would see an exited container). Confirm that is the intended model; docs/README call it "periodic job scheduler".
2. **`BELUNO_ENVIRONMENT=staging` cannot be used with least-privilege DSNs.** `assert_runtime_requirements` demands all four DB URLs (including the migrator's) in EVERY process, plus `sslmode=require`, SMTP, https OTLP. So the stack defaults to `development` (plain-text bundled Postgres/collector) and gives each process only its own DSN. For a real staging host with a managed DB, either all four DSNs would have to be handed to the API (migrator credentials in the API container) or the app check should be per-process. Suggest an app change; not made here.
3. **300 ms is not a histogram bucket.** SDK default bounds are ... 250, 500, 750, 1000 ms, so p95 near 300 ms is interpolated; p95 SLO alerts are approximate. An OTel View with explicit bounds including 300 would fix it (app change in `observability/`).
4. Traces from the app are accepted by the collector and discarded (`debug` exporter, basic) until a trace backend is chosen; the app always exports spans to the same endpoint, so a missing traces pipeline would log export errors.
5. Postgres is pinned to 16-alpine to match the test suite and `docker-compose.test.yml`; the user's local dev cluster is PG18. Decide whether staging should be 18 (note data dir path changes in the PG18 image).
6. Alertmanager has a placeholder receiver only; nobody is notified until a real channel is configured.
7. `.github/workflows/backend-ci.yml`: added top-level `permissions: contents: read` (affects `quality` only by tightening the token). Remove if undesired.
8. Grafana/Prometheus/Alertmanager/collector images are pinned to tags (verified to exist on Docker Hub today), not digests.

Runbook notes for the controller: use the "Running the stack" and "Alerts" sections above; the metric table is the reference for `docs/runbooks/sync-operations.md`; the `sync-operations.md` metric-name column uses OTel names, the Prometheus names differ as listed here.
