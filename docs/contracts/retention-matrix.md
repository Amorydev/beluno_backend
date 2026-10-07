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
| Expired email and passkey challenges, refresh tokens, rate-limit windows | 1 day after expiry | hourly `iam.purge_expired_auth_records` |
| Passkeys (public keys, counters, labels) | until removed by the person or the account is deleted | `DELETE /v1/me/passkeys/{id}`; `iam.forget_actor_credentials` on account deletion |
| Sessions (device label, platform, app version) and their refresh tokens | 30 days after revocation or expiry | hourly `iam.purge_expired_auth_records` through `iam.purge_stale_sessions` |
| Plans scheduled for deletion, with everything they hold (participants, invites, finance history, feed events, plan-scope change rows) | `BELUNO_PLAN_PURGE_AFTER_DAYS` (30) after deletion is scheduled; restorable until then | daily `plans.purge_deleted` through `plans.purge_deleted_plan` (refuses cutoffs under 7 days); former participants get a `plan_access` delete; audit events (IDs only) stay |
| Private packing items | live with their plan; move with a guest who claims an account; deleted with their owner's account | purged with the plan (owners' user scopes get a `packing_item` delete); `coordination.forget_private_packing` on account deletion |
| Booking secrets (sealed confirmation codes and private notes) | live with the booking and its plan; cleared when set to null | purged with the plan; never logged, synced, or stored in replayable responses |
| Exports (plan CSV/JSON, account JSON) | never stored: built per request and sent with `Cache-Control: no-store` | each export leaves an audit event (`plan.exported`, `account.exported`) with no content |
| Problem reports (the person's words, diagnostic snapshot) | until the person deletes their account, or a legal policy sets a limit | `analytics_ops.forget_problem_reports` on account deletion; snapshots hold no expense text, notes, names, or codes |
| Media files (receipts, covers, memories) | live with their plan until deleted; a deleted account's memories go with it (receipts stay); the unscanned upload is deleted once scanned (and again 15 minutes later, after its upload URL expires); uploads never reported done are dropped after 7 days | a trigger (delete) or the plan purge queues `incoming/` and `media/` objects in `media_memories.object_deletions`; the worker's `media.delete_objects` removes due objects every 10 minutes and `media.sweep` handles stuck or abandoned uploads hourly |
| Push tokens and notification settings | tokens live with their session (removed when it is revoked); settings until the account is deleted | session revoke trigger; `engagement.forget_settings` on account deletion |
| Notifications (outbox) | 30 days after delivery, skipping, or failure | `notifications.dispatch` purges them every minute |
| Deleted crews | name and members cleared at deletion; the tombstone stays for sync | `crew.delete` and account deletion |
| Failed jobs (dead letters) | until replayed or removed by an operator | `scripts/jobs.py` |

Configuration refuses a change or operation retention shorter than the offline
window. Final legal retention and regional policy remain a product/operations
decision before public launch; shortening retention below the offline window is
never acceptable.
