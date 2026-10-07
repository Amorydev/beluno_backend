# Retention Matrix

| Data | Retention | Mechanism |
|---|---|---|
| Supported offline window | 90 days (`BELUNO_SYNC_OFFLINE_WINDOW_DAYS`) | Sessions live 90 days idle / 180 days absolute; cursors older than a scope's compaction floor must bootstrap |
| `sync_audit.change_log` rows and tombstones | ≥ 180 days (`BELUNO_SYNC_CHANGE_RETENTION_DAYS`) | Daily `sync.compact_changes` job; the SQL gate refuses cutoffs inside the offline window; the floor rises per scope |
| `sync_audit.operations` (idempotent outcomes) | ≥ 180 days (`BELUNO_SYNC_OPERATION_RETENTION_DAYS`) | Daily `sync.purge_operations` job; records may be replayed until purged. Stored responses can outlive a deleted account or a purged plan by up to this long |
| `activity.events` (feed) | ≥ 180 days (`BELUNO_SYNC_CHANGE_RETENTION_DAYS`) | Daily `activity.purge_events` job; older events removed in batches alongside change-log compaction |
| `sync_audit.audit_events` | indefinite until a legal policy is set | never deleted by application code |
| Finance history (`finance.*` revisions, payers, splits, refunds, settlements, transactions, postings, FX snapshots, fund movements) | lives with its plan (purged with a deleted plan) | append-only: triggers reject UPDATE/DELETE for every role except the plan purge gate; runtime roles hold no DELETE grant |
| `finance.account_balances` (projection) | lives with the ledger | rebuilt from postings by `scripts/finance.py rebuild`; never the source of truth |
| Expired email challenges, refresh tokens, rate-limit windows | 1 day after expiry | hourly `iam.purge_expired_auth_records` |
| Sessions (device label, platform, app version) and their refresh tokens | 30 days after revocation or expiry | hourly `iam.purge_expired_auth_records` through `iam.purge_stale_sessions` |
| Plans scheduled for deletion, with everything they hold (participants, invites, finance history, feed events, plan-scope change rows) | `BELUNO_PLAN_PURGE_AFTER_DAYS` (30) after deletion is scheduled; restorable until then | daily `plans.purge_deleted` through `plans.purge_deleted_plan` (refuses cutoffs under 7 days); former participants get a `plan_access` delete; audit events (IDs only) stay |
| Booking secrets (sealed confirmation codes and private notes) | live with the booking and its plan; cleared when set to null | purged with the plan; never logged, synced, or stored in replayable responses |
| Deleted crews | name and members cleared at deletion; the tombstone stays for sync | `crew.delete` and account deletion |
| Failed jobs (dead letters) | until replayed or removed by an operator | `scripts/jobs.py` |

Configuration refuses a change or operation retention shorter than the offline
window. Final legal retention and regional policy remain a product/operations
decision before public launch; shortening retention below the offline window is
never acceptable.
