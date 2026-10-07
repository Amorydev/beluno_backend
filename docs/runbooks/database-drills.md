# Runbook: Database Drills

Backup, restore, and migration rehearsal for the PostgreSQL database, with the
timings that feed recovery targets. `scripts/db_drill.py` runs the drills; it
works on scratch copies and never writes to the source database.

## What the drills check

- **Backup and restore:** `pg_dump -Fc` of the source, a restore into a fresh
  database with owners and grants intact, every sync scope generation bumped
  (`sync-operations.md`, "Database restore"), and a ledger reconciliation of every
  plan on the restored data. Row counts per table must match the source exactly.
- **Migration rehearsal:** a restored copy is upgraded to head the way a deploy
  does (`scripts/bootstrap_database.py`: migrations, queue schema, runtime
  grants). Row counts must not drop, security facts must not weaken (tables with
  row-level security, policies), no constraint may be left unvalidated, no index
  invalid, and every ledger must reconcile.

## The tool

```bash
uv run python scripts/db_drill.py backup-restore --source-db <database>
uv run python scripts/db_drill.py rehearse-migration --source-db <database> [--dump FILE]
```

| Option | Environment variable | Role | Used for |
|---|---|---|---|
| `--admin-dsn` | `BELUNO_DRILL_ADMIN_DSN` | superuser | dump, create and drop scratch databases, restore, row counts |
| `--migrator-dsn` | `BELUNO_MIGRATION_DATABASE_URL` | `migrator` | bumping generations, migrating the copy |
| `--worker-dsn` | `BELUNO_WORKER_DATABASE_URL` | `worker_runtime` | ledger reconciliation |

The database name inside each DSN is replaced by the database being worked on.
Passwords reach the PostgreSQL tools through environment variables, never the
command line, and are never printed.

- The dump runs as a superuser because the tables use row-level security, which an
  ordinary role cannot read through `pg_dump`.
- The runtime roles must already exist on the cluster, as in any restore: ownership
  and grants are restored with the data, and the script recreates the database-level
  connect grants.
- `pg_dump` and `pg_restore` must be the same major version as the server (or newer).
- The scratch database and the dump are removed at the end. The dump holds personal
  data: use `--keep` only when you must inspect it, and delete it afterwards.
- The exit code is non-zero when a row count differs, a validation fails, or any
  ledger reports drift.

Two helper modes exist for disposable clusters only. `provision` creates the
runtime roles (with the synthetic test passwords), a database, and migrates it, with
`--revision REV` to stop at an older revision; `drop` removes a database.

## Local drill

Local drill (single laptop, local PostgreSQL 18.3). Measured on 2026-10-07 on the
database left by the load baseline (`performance.md`): 254 MB on disk, about
599,000 rows in 45 tables, 29 plans, 165 sync scopes. The dump
is 28.7 MiB.

```bash
export BELUNO_DRILL_ADMIN_DSN='postgresql://<superuser>:<password>@localhost:<port>/postgres'
export BELUNO_MIGRATION_DATABASE_URL='postgresql+psycopg://migrator:<password>@localhost:<port>/beluno_load'
export BELUNO_WORKER_DATABASE_URL='postgresql+psycopg://worker_runtime:<password>@localhost:<port>/beluno_load'
uv run python scripts/db_drill.py backup-restore --source-db beluno_load
uv run python scripts/db_drill.py rehearse-migration --source-db beluno_load
```

### Backup and restore (three runs)

| Step | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| `pg_dump -Fc` | 1.58 s | 1.69 s | 1.55 s |
| create database | 0.11 s | 0.08 s | 0.10 s |
| `pg_restore` | 3.00 s | 3.84 s | 8.20 s |
| bump sync generations (165 scopes) | 0.01 s | 0.01 s | 0.01 s |
| reconcile 29 ledgers | 0.11 s | 0.10 s | 0.09 s |
| Dump, restore, bump, reconcile | 4.7 s | 5.6 s | 9.9 s |

Row counts matched the source in every run, and reconciliation found no drift. As a
negative control, a balance changed by one unit in a restored copy was reported as
`balance_drift` by the same reconcile step.

The restore time varied most (3 to 8 seconds) because the laptop was shared; plan
with the slow end. Restore speed here is about 30 to 85 MB of database per second.
That is a local-disk figure: measure on the staging host before using it to
estimate recovery time for a larger database.

### Migration rehearsal

| Case | Dump | Restore | Migrate | Validate | Total (with create) | Result |
|---|---|---|---|---|---|---|
| Copy at head (the 254 MB database) | 1.55 s | 3.86 s | 0.75 s | 0.18 s | 6.4 s | OK, nothing to apply |
| Copy two revisions behind (`000008` to head, 558 rows) | 0.09 s | 0.13 s | 0.83 s | 0.05 s | 1.2 s | OK, RLS tables 39 to 40, policies 84 to 85 |

The second case proves the path (revision before and after, the source left at its
old revision, structure checks) but says nothing about lock time on real data.
Before a release with a migration, run the rehearsal on a restored production-sized
dump and read its "migrate" timing together with the migration's header (lock and
scan risk).

## Recovery targets: what the drill gives you

- **RPO** is the interval between backups (plus archived WAL when point-in-time
  recovery is configured). `pg_dump` is a logical backup: it captures one moment, so
  with nightly dumps the worst case is a day of changes. Devices keep their outbox
  and replay it after a restore, which narrows the loss for work done offline.
- **RTO** is detection, decision, and the measured steps: restore, bump, reconcile,
  validation. The drill measures restore, bump, reconcile, and counting. For the
  local database that is 5 to 10 seconds; add the time to stop and restart
  processes and repoint connections.
- Record the staging numbers beside the local ones once staging exists, then derive
  the targets with a safety factor (at least 2x on restore time).

## Restore procedure (staging or production)

Restore into a new database, check it, then switch the connection settings. Keep the
damaged database untouched until the incident is closed.

1. **Decide and announce.** Name an incident lead. Pick the backup (the newest
   one that predates the damage) and note its age: that is the data lost.
2. **Stop the write path.** Set the kill switches in `deploy-rollback.md`
   (`BELUNO_FINANCE_WRITES_ENABLED=false`, `BELUNO_SYNC_PUSH_ENABLED=false`, and the
   invite and claim switches) and restart the API, so clients keep their queues.
   Other mutations (plans, profile) have no global switch, so then stop the API,
   worker, and scheduler processes entirely, or answer `503` at the proxy, until the
   restore is verified. A running process would write into the database being
   replaced.
3. **Create the target.** Create an empty database with the connect grants for the
   four runtime roles (`create_database` in `scripts/db_drill.py` shows the
   statements) and confirm the roles exist.
4. **Restore.** As the superuser, `pg_restore --exit-on-error --single-transaction
   --dbname=<new database> <dump>`. Do not use `--no-owner` or `--no-acl`: the
   `migrator` role must own the objects and the runtime grants must come back.
5. **Check the structure.** Compare row counts with the backup's, and confirm the
   alembic revision matches the release you are about to run (migrate forward with
   `scripts/bootstrap_database.py` if the backup is older).
6. **Bump every sync generation** as the migrator, once:
   `UPDATE sync_audit.scope_heads SET generation = generation + 1;`
   Devices then answer `resync_required` for every scope and bootstrap again;
   queued operations are replayed through push.
7. **Reconcile every ledger** with the worker role:
   `uv run python scripts/finance.py reconcile` (exit code 1 when anything drifts).
   Resolve findings per `finance-operations.md` before reopening finance writes.
   A backup taken before a purge still holds the plans purged since: the worker's
   daily `plans.purge_deleted` job removes them again on its next run (their
   deletion dates are kept, so nothing new becomes restorable). Account deletions
   made after the backup are lost with it; re-run them from the incident log.
8. **Switch.** Point `BELUNO_*_DATABASE_URL` of every process at the new database
   (secret store), start the worker and scheduler, then the API with the finance
   switch still off.
9. **Reopen in stages.** Check `/health/ready`, a handshake and a pull from a test
   account, and the job queue (`scripts/jobs.py health`). Then re-enable finance
   writes, sync push, and invites, watching the error rate and the
   `resync_required` rate on the Grafana overview.
10. **Afterwards.** Keep the damaged database for forensics (restrict access), record
    the timings of this run beside the drill numbers, and write the incident
    report. Dumps and restored copies contain personal data: delete what you no
    longer need.

Never repair a restored or live database by truncating ledger, audit, change-log,
idempotency, or tombstone tables.

## Migration rehearsal procedure (before each release with a migration)

1. Take a fresh dump of production (or use last night's) and restore it where
   production data may live (staging, with equivalent access controls).
2. `uv run python scripts/db_drill.py rehearse-migration --source-db <production-like database>`
   against that cluster; add `--dump FILE` to reuse a dump. It restores into a scratch
   copy and leaves the source alone.
3. Read the result: revision before and after, per-schema row counts, the structure
   signals, reconciliation, and the "migrate" timing. A migration that holds a lock
   longer than the API's request timeout needs a different rollout.
4. Compare the timing with the migration's header (lock and scan risk), then run
   the real migration through the deploy runbook.
