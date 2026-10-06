# Retention Matrix

| Data | Retention | Mechanism |
|---|---|---|
| Supported offline window | 90 days (`BELUNO_SYNC_OFFLINE_WINDOW_DAYS`) | Sessions live 90 days idle / 180 days absolute; cursors older than a scope's compaction floor must bootstrap |
| `sync_audit.change_log` rows and tombstones | ≥ 180 days (`BELUNO_SYNC_CHANGE_RETENTION_DAYS`) | Daily `sync.compact_changes` job; the SQL gate refuses cutoffs inside the offline window; the floor rises per scope |
| `sync_audit.operations` (idempotent outcomes) | ≥ 180 days (`BELUNO_SYNC_OPERATION_RETENTION_DAYS`) | Daily `sync.purge_operations` job; records may be replayed until purged |
| `sync_audit.audit_events` | indefinite until a legal policy is set | never deleted by application code |
| Finance history (`finance.*` revisions, payers, splits, refunds, settlements, transactions, postings, FX snapshots, fund movements) | indefinite until a legal policy is set | append-only: triggers reject UPDATE/DELETE for every role; runtime roles hold no DELETE grant |
| `finance.account_balances` (projection) | lives with the ledger | rebuilt from postings by `scripts/finance.py rebuild`; never the source of truth |
| Expired email challenges, refresh tokens, rate-limit windows | 1 day after expiry | hourly `iam.purge_expired_auth_records` |
| Failed jobs (dead letters) | until replayed or removed by an operator | `scripts/jobs.py` |

Configuration refuses a change or operation retention shorter than the offline
window. Final legal retention and regional policy remain a product/operations
decision before public launch; shortening retention below the offline window is
never acceptable.
