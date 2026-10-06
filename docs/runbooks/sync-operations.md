# Sync and Jobs Operations Runbook

## Signals to watch

All metrics are OpenTelemetry instruments exported through OTLP when
`BELUNO_OTEL_EXPORTER_OTLP_ENDPOINT` is set (API and worker processes). Attribute
values are command names, scope types, and outcomes only.

| Metric | Type | Attributes | Dashboard / alert |
|---|---|---|---|
| `beluno.commands` | counter | `command`, `source` (http/push), `outcome` (applied, replayed, or the error code in lower case) | Error-rate panel per command; alert when `version_conflict`, `idempotency_key_reused`, or `rate_limited` jumps |
| `beluno.command.duration` | histogram (ms) | `command` | P95 per command; sync-after-connect budget |
| `beluno.command.retries` | counter | `command` | Serialization/deadlock retries; alert on sustained growth (hot plan head) |
| `beluno.sync.push.results` | counter | `outcome` | Share of `retry`/`skipped`/`conflict` results |
| `beluno.sync.pull.pages` | counter | `scope_type`, `status` | `resync_required` rate (compaction floor, restores, access changes) |
| `beluno.sync.pull.items` | counter | `scope_type` | Pull bytes/volume proxy |
| `beluno.sync.changes.appended` | counter | – | Change-log growth; capacity planning |
| `beluno.sync.changes.compacted` | counter | – | Confirms the daily retention job runs |
| `beluno.sync.operations.purged` | counter | – | Confirms the daily idempotency purge runs |
| `beluno.jobs.queue` | gauge | `measure` (todo, doing, failed, oldest_waiting_seconds) | Alert: `failed` > 0 for 15 min, `oldest_waiting_seconds` > 300 |

The worker refreshes `beluno.jobs.queue` every five minutes through the
`jobs.report_queue_health` periodic task.

## Dead letters

A job that exhausted its retries stays in `jobs.procrastinate_jobs` with
`status = 'failed'`; that is the dead-letter queue. Handlers are idempotent and
take identifiers only, so a replay is always safe.

```bash
uv run python scripts/jobs.py list --status failed
uv run python scripts/jobs.py retry 1234 --operator jane
uv run python scripts/jobs.py health
```

Every replay writes an audit event (`job.retried`) with the job ID, task name,
and operator. Fix the cause before replaying a job that failed repeatedly.

## Retention and compaction

- Offline window: `BELUNO_SYNC_OFFLINE_WINDOW_DAYS` (90).
- Change rows and tombstones: `BELUNO_SYNC_CHANGE_RETENTION_DAYS` (180).
- Idempotency records: `BELUNO_SYNC_OPERATION_RETENTION_DAYS` (180).

The daily `sync.compact_changes` job deletes change rows older than the
retention cutoff in committed batches and raises `scope_heads.floor_seq`; a
cursor below the floor receives `resync_required` and bootstraps again. The SQL
gate refuses cutoffs inside the 90-day offline window. Never delete change,
operation, or head rows by hand as a rollback.

## Database restore

After restoring PostgreSQL from a backup, cursors held by clients may point
past the restored heads. Bump every scope generation once as the migrator so
all devices resync from a snapshot:

```sql
UPDATE sync_audit.scope_heads SET generation = generation + 1;
```

Devices then receive `resync_required` for every scope and re-bootstrap. Their
queued operations are replayed through push; operations the restore forgot are
applied again, operations it kept are replayed from the operation records.

## Kill switches

| Setting | Effect |
|---|---|
| `BELUNO_SYNC_PUSH_ENABLED=false` | `/v1/sync/push` returns `503 FEATURE_DISABLED`; clients keep their queues |
| `BELUNO_SYNC_PULL_ENABLED=false` | `/v1/sync/pull` returns `503 FEATURE_DISABLED`; the handshake still reports it |
| `BELUNO_SYNC_DISABLED_COMMANDS='["plan.duplicate"]'` | The command is refused on REST (`503`) and push (`retry`) alike |

Disable a command rather than deleting accepted operations or change rows.
