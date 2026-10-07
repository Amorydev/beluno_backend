# Runbook: Performance and Load

Service levels, the load harness that checks them, and the results so far. The
harness is Locust (`tests/load/`) plus a seed script (`scripts/load_seed.py`);
nothing here touches production.

## Service levels

Release 1 is sized for about 5k daily active users. The load target is twice the
forecast peak: about 50 requests per second across the mix below.

| Scenario | Request name in Locust | p95 target |
|---|---|---|
| Quick expense | `quick expense` | 300 ms |
| Open a 10-day, 6-person trip (about 150 expenses): every page of a first sync | `pull trip bootstrap` | 1 s |
| Settle up: read the suggestions, record a payment | `settle up preview`, `settle up record` | 300 ms each |
| 20 people writing to one plan at once (ledger-head contention) | `busy plan expense` | 1 s |
| Error rate over a run | all requests | below 0.5 % |

Also reported without a target: `pull trip incremental` (catching up from a stored
cursor) and `busy plan pull` (a writer catching up on the busy plan).

`pull trip bootstrap` is one sample per trip, not per page: a 150-expense trip
is two pages (about 310 changes, 370 KB), and the sample covers both.

## Harness

- `scripts/load_seed.py` refuses to run when `BELUNO_ENVIRONMENT=production`. It
  creates registered users and live sessions straight in the database (migration
  role) and signs access tokens with the configured key; the application has no
  bypass. Plans, invites, and expenses go through the HTTP API, so ledger
  invariants hold. It writes a manifest with live bearer tokens (mode 0600, one
  hour by default): never commit it, delete it after the run.
- `tests/load/locustfile.py` has one user class per scenario. Pass its name to run
  it alone; pass none for the mixed run. It names requests as in the table above
  and sends a fresh `Idempotency-Key` on every write, as the apps do. No scenario
  uses `If-Match`: creating expenses and settlements is not versioned.
- Seeded data (defaults): 8 trips with 6 people and 150 expenses each (USD, EUR,
  and JPY; equal, exact, percentage, and weighted splits; some with two payers),
  12 small plans for quick expenses, 8 plans for settling up, and one plan with 20
  registered writers.
- Finance writes are limited to 120 per minute per person per plan. The scenarios
  stay below it (a person averages about one write per second at most); a run that
  exceeds it reports `429` as failures, which is correct behaviour, not a bug.
- `tests/load/` is not collected by pytest: its files do not match `test_*.py`.

## Local baseline

Local baseline (single laptop, local PostgreSQL). Measured on 2026-10-07.

- Machine: Apple M5 Pro (15 cores), 24 GB, macOS 26.4. API, PostgreSQL, and Locust
  shared it with other work (load average 4 to 8 during the runs), so single
  numbers move by up to a factor of two between identical runs.
- API: `uvicorn` with 4 workers, `BELUNO_ENVIRONMENT=development`, one pool of up
  to 10 connections per worker. PostgreSQL 18.3 with default settings
  (`shared_buffers` 128 MB, `synchronous_commit` on), database on the same host.
- Locust 2.46, one process, no network between the load generator and the API.
- Every scenario ran 3 minutes (mixed: 5 minutes) after a ramp; statistics reset
  once all users were running. Scenarios ran one after another on the same
  database, which grew from about 1,300 to about 36,000 expenses by the end (plans used by
  the pull scenario stay at 150 expenses each, because nothing writes to them).

Times are milliseconds. Locust rounds percentiles to two significant digits.

### One scenario at a time

| Request | Users | rps | p50 | p95 | p99 | Errors | p95 target | Result |
|---|---|---|---|---|---|---|---|---|
| `quick expense` | 40 | 20.1 | 53 | 110 | 140 | 0 / 3,540 | 300 | within |
| `pull trip bootstrap` | 20 | 2.7 | 76 | 130 | 180 | 0 / 475 | 1,000 | within |
| `pull trip incremental` | 20 | 7.4 | 15 | 25 | 43 | 0 / 1,315 | none | n/a |
| `settle up preview` | 10 | 5.0 | 14 | 18 | 35 | 0 / 895 | 300 | within |
| `settle up record` | 10 | 5.0 | 26 | 32 | 42 | 0 / 895 | 300 | within |
| `busy plan expense` | 20 | 17.5 | 30 | 60 | 93 | 0 / 3,152 | 1,000 | within |
| `busy plan pull` | 20 | 1.7 | 39 | 80 | 87 | 0 / 304 | none | n/a |

### Mixed run at the 2x target

80 users for 5 minutes: 20 busy-plan writers plus quick expense, trip pull, and
settle-up users in a 3:2:1 ratio.

| Request | rps | p50 | p95 | p99 | Errors | p95 target | Result |
|---|---|---|---|---|---|---|---|
| `quick expense` | 15.0 | 110 | 190 | 240 | 0 / 4,395 | 300 | within |
| `pull trip bootstrap` | 2.5 | 130 | 200 | 250 | 0 / 737 | 1,000 | within |
| `pull trip incremental` | 7.5 | 22 | 59 | 91 | 0 / 2,193 | none | n/a |
| `settle up preview` | 5.0 | 27 | 67 | 110 | 0 / 1,465 | 300 | within |
| `settle up record` | 5.0 | 68 | 140 | 170 | 0 / 1,465 | 300 | within |
| `busy plan expense` | 17.4 | 32 | 110 | 170 | 0 / 5,089 | 1,000 | within |
| `busy plan pull` | 1.8 | 35 | 79 | 96 | 0 / 514 | none | n/a |
| All requests | 54.2 | 50 | 160 | 210 | 0 / 15,858 | error rate < 0.5 % | within |

### Headroom: twice the target

The same mix with 160 users (about 100 rps in total, four times the forecast
peak), 3 minutes. This is a margin check, not a service level.

| Request | rps | p50 | p95 | p99 | Errors | Against the p95 target |
|---|---|---|---|---|---|---|
| `quick expense` | 35.0 | 250 | 380 | 440 | 0 / 6,034 | over (300) |
| `pull trip bootstrap` | 5.8 | 260 | 370 | 420 | 0 / 1,000 | within (1,000) |
| `settle up preview` | 11.6 | 68 | 150 | 200 | 0 / 1,991 | within (300) |
| `settle up record` | 11.5 | 170 | 290 | 360 | 0 / 1,985 | at the limit (300) |
| `busy plan expense` | 17.0 | 35 | 240 | 330 | 0 / 2,929 | within (1,000) |
| All requests | 100.5 | 140 | 340 | 400 | 0 / 17,304 | no errors |

### Reading the numbers

- Every service level holds at the 2x target (about 54 rps), with no errors.
- The margin is roughly 2x beyond that on this setup: at 100 rps the quick
  expense p95 passes 300 ms and settle-up recording sits on it. The first limit
  is the API process (Python, four workers) rather than the database: sampling
  `pg_stat_activity` during a quick-expense run showed the API sessions idle
  waiting for the client almost all the time. More API workers or hosts are the
  lever; recheck on staging hardware before relying on this.
- Twenty concurrent writers on one plan stay far below the 1 s target (p95 60 to
  110 ms at about 17 writes per second). Ledger-head serialization is not a
  bottleneck at this scale.
- A freshly seeded database has no planner statistics, so the seed script runs
  `ANALYZE`. The first quick-expense run, made before autovacuum had analyzed the
  new tables, had p50 100 ms and p95 230 ms; the rerun after had 53 ms and 110 ms.
  Host load may explain part of that gap.
- Single runs vary because the host was shared. Judge trends over several runs, and
  prefer the staging numbers for decisions.

## Reproduce locally

A disposable PostgreSQL cluster and a superuser DSN are needed. The provision step
creates the four runtime roles with the synthetic test passwords, so never run it
against a real cluster.

```bash
export BELUNO_DRILL_ADMIN_DSN='postgresql://<superuser>:<password>@localhost:<port>/postgres'

# 1. Database with roles and migrations (prints no credentials)
uv run python scripts/db_drill.py provision --database beluno_load

# 2. Keys and settings for the API and the seed script (never written to the repository)
eval "$(uv run python scripts/generate_signing_key.py --with-token-hash-key | sed 's/^/export /')"
export BELUNO_ENVIRONMENT=development
export BELUNO_API_DATABASE_URL='postgresql+psycopg://api_runtime:<api password>@localhost:<port>/beluno_load'
export BELUNO_MIGRATION_DATABASE_URL='postgresql+psycopg://migrator:<migrator password>@localhost:<port>/beluno_load'

# 3. API with several workers
uv run uvicorn beluno.api.main:app --host 127.0.0.1 --port 8000 --workers 4 --log-level warning &

# 4. Seed (about 30 seconds) and write the manifest
uv run python scripts/load_seed.py --base-url http://127.0.0.1:8000 \
  --manifest "$TMPDIR/beluno-load-manifest.json"

# 5. One scenario each, then the mixed run
M="$TMPDIR/beluno-load-manifest.json"
uv run locust -f tests/load/locustfile.py --headless --host http://127.0.0.1:8000 \
  --manifest "$M" -u 40 -r 10 -t 3m --reset-stats --csv /tmp/quick QuickExpenseUser
#   TripPullUser: -u 20      SettleUpUser: -u 10      BusyPlanWriterUser: -u 20
#   mixed (all classes):  -u 80 -r 10 -t 5m
#   headroom:             -u 160 -r 20 -t 3m

# 6. Clean up
kill %1; rm "$M"
uv run python scripts/db_drill.py drop --database beluno_load
```

The role passwords are in `src/beluno/testkit/environment.py`
(`RUNTIME_PASSWORDS`). The `--csv` files hold one row per request name with the
percentiles used in the tables above.

## Run it on staging

1. Deploy the release under test with `BELUNO_ENVIRONMENT=staging` (see
   `deploy-rollback.md`). The seed script works in staging and development only.
2. On a machine that can reach the API and the database, set the same settings the
   API uses for signing (`BELUNO_AUTH_SIGNING_KEYS`, `BELUNO_TOKEN_HASH_KEY`) and
   `BELUNO_MIGRATION_DATABASE_URL`. The migration role writes the users and
   sessions; the seed also clears rate-limit counters between plans, which the
   same role may do.
3. Run `scripts/load_seed.py --base-url https://<staging host>` and the Locust
   commands above with `--host https://<staging host>`. Generate load from a
   separate host (not the API host) and, for the headroom run, split users across
   several Locust workers (`--master` and `--worker`) so the generator is not the
   limit. The manifest tokens last an hour: for a longer run, refresh them with
   `scripts/load_seed.py --remint <manifest>` between runs.
4. Unauthenticated limits key on the client address: behind a load balancer the
   API needs `--proxy-headers` (see `deploy-rollback.md`), otherwise every request
   shares one bucket.
5. Compare against the Grafana overview during the run (command p95, ledger lock
   wait, job queue age, error rate) and record a new table above, labelled with
   the host size and region.
6. The seeded users, plans, and expenses stay in the staging database. Keep them
   for later runs, or drop and recreate the staging database before the soak with
   the Android app.
