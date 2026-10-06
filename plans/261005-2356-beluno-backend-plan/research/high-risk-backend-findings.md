# High-risk backend findings — Beluno

> Phạm vi: financial ledger, offline sync, auth/RBAC/invite, privacy/media,
> notifications, observability, testing/chaos, backup/DR, rate limits và launch
> gates. Đây là các quality gates nội bộ cho **một unified product scope**;
> mọi capability cùng thuộc một definition of done.

## 1. Quyết định nền tảng cần khóa trước khi code

1. Dùng modular monolith + PostgreSQL; tất cả financial writes đi qua một
   application service duy nhất, không cho client ghi trực tiếp các bảng ledger.
2. Expense, split, FX và settlement là lịch sử bất biến: sửa bằng revision,
   void hoặc reversal; không overwrite dữ liệu tài chính đã được đồng bộ.
3. Local SQLite là source cho UI; PostgreSQL là authority giữa các thiết bị.
   Outbox local và server change log là contract bắt buộc, không phải tối ưu sau.
4. Role chỉ là một đầu vào. Quyền thực tế là relationship/attribute-based:
   `actor × plan membership × role × resource visibility × resource state × action`.
5. Plan fund là một ledger account riêng theo currency, không đồng nghĩa app giữ
   tiền thật. Custodian chỉ là metadata, không phải chủ sở hữu số tiền.
6. Database backup, object/media backup, key/config backup là ba luồng riêng.
   Khả năng restore phải được đo, không suy ra từ việc “backup successful”.

## 2. Financial ledger correctness

### 2.1 Canonical model

Nên thêm một lớp canonical postings có thể rebuild balance:

- `ledger_transactions`: expense revision, refund, settlement, reversal, fund
  contribution/withdrawal; có `plan_id`, `currency_code`, `status`,
  `algorithm_version`, `effective_at`, audit metadata.
- `ledger_postings`: nhiều dòng cho một transaction, gồm `account_id`,
  `amount_minor` signed, currency. Account gồm member account và fund account.
- Các bảng `expenses`, `expense_payers`, `expense_splits`, `settlements` giữ
  domain representation; postings là canonical accounting result.
- `member_balances` và summaries chỉ là projection. Phải xóa/rebuild được hoàn
  toàn từ committed ledger.

Quy ước dấu nên cố định: `net > 0` là người được nhận tiền.

```text
expense posting for member = paid_minor - owed_minor
settlement debtor -> creditor:
  debtor   +amount_minor
  creditor -amount_minor
```

Settlement suggestion/debt simplification chỉ là derived view; chỉ settlement
được người dùng xác nhận mới tạo ledger transaction.

### 2.2 Hard invariants

- `amount_minor` là signed/unsigned `BIGINT` theo ngữ cảnh; không dùng float.
- Currency exponent lấy từ bảng metadata được pin/versioned; không mặc định 2
  chữ số (JPY = 0, KWD = 3). Reject precision ngoài exponent.
- Mỗi expense revision: `Σ payer = amount` và `Σ owed = amount` trong **original
  currency**.
- Mỗi ledger transaction/currency: `Σ postings = 0`.
- Mỗi plan/currency: `Σ member/fund net = 0`.
- Một member chỉ xuất hiện tối đa một lần trong payer allocation và split result.
- Percentage/weight dùng integer hoặc rational; không lưu IEEE floating point.
- Remainder allocation deterministic bằng largest remainder + immutable stable
  tie-break (member ID hoặc captured participant order).
- Store `split_algorithm_version`, inputs và resolved owed amounts. Thay đổi code
  sau này không được silently đổi lịch sử.
- Refund/reversal tham chiếu original transaction; tổng refund không vượt phần
  refundable trừ khi có explicit adjustment permission.
- Settled entry không bị sửa ngầm. Edit ảnh hưởng settlement phải tạo revision,
  trạng thái “reopened/disputed” và audit event.
- Original amount/currency luôn là truth; base amount chỉ là snapshot phục vụ
  display/budget.
- FX snapshot bắt buộc có direction rõ (`base/quote`), high-precision decimal,
  timestamp, provider/manual source, actor, rounding mode và rate version.
- Không net chéo currency nếu chưa có explicit conversion transaction + FX
  snapshot. Settlement được ghi theo currency thực tế.
- Kiểm tra overflow trước multiply/divide, amount bounds theo product policy.
- Financial hard delete bị cấm; chỉ void/reversal và retention-controlled audit.

### 2.3 Transaction/concurrency pattern

Financial command phải thực hiện trong một database transaction ngắn:

1. insert/lock idempotency record;
2. authorize against current membership;
3. lock `plan_ledger_head` hoặc dùng `SERIALIZABLE`;
4. validate expected entity version;
5. validate money/splits/FX/member references;
6. persist revision + allocations + postings + audit;
7. update ledger revision, projections và server change log;
8. persist canonical response for idempotent replay;
9. commit.

Nếu dùng PostgreSQL `SERIALIZABLE`, bắt buộc có bounded retry cho SQLSTATE
`40001`; không retry validation/authorization errors. Một row
`plan_ledger_head` được `SELECT ... FOR UPDATE` thường dễ vận hành hơn cho small
team và làm ledger sequence deterministic.

Cross-row sums không thể được đảm bảo đáng tin cậy bằng row-local `CHECK` đơn
thuần. Dùng stored function/service transaction, revoke direct table writes,
thêm deferred constraint trigger nếu đội đủ năng lực, và reconciliation job độc
lập. DB constraints vẫn phải cover `NOT NULL`, FK, uniqueness, currency/amount
bounds và valid status transitions.

### 2.4 Idempotency contract

- Mọi mutation có `operation_id` client-generated; financial create/reversal có
  thêm `Idempotency-Key`.
- Unique scope: `(actor_id, endpoint_scope, idempotency_key)`; lưu request hash.
- Same key + same hash trả nguyên status/body/resource đã commit.
- Same key + different hash trả `409 IDEMPOTENCY_KEY_REUSED`.
- Idempotency record được commit cùng domain write; không ghi cache ngoài
  transaction rồi mới ghi ledger.
- Retention phải dài hơn maximum supported offline duration + retry safety
  window. Đề xuất support offline 90 ngày, giữ financial idempotency/tombstone ít
  nhất 180 ngày. Client quá floor phải full-resync, không replay mù.
- Unique client resource ID là lớp chống duplicate thứ hai.

### 2.5 Failure modes cần test

- response mất sau commit rồi client retry;
- hai device edit cùng revision;
- app/server chết giữa allocations và projection;
- retry sau token/member bị revoke;
- serializable failure/deadlock;
- stale/manual/reversed FX direction;
- split 1 minor unit cho N người; negative/refund/large amount/overflow;
- migration đổi currency exponent hoặc rounding algorithm;
- projection drift dù source ledger đúng;
- expense void sau partial settlement;
- fund contribution và fund-paid expense bị double counted.

## 3. Offline-first sync

### 3.1 Operation/change envelopes

Client outbox operation nên có:

```text
operation_id, idempotency_key, actor_id, device_id, plan_id,
command_type, command_schema_version, entity_id, expected_entity_version,
dependency_operation_ids, payload, client_created_at
```

Server change envelope nên có:

```text
server_seq, plan_id, entity_type, entity_id, operation,
entity_version, changed_at, minimal payload/pointer
```

`server_seq` là ordering authority; device clock chỉ là metadata. Cursor nên per
plan/device hoặc account scope rõ ràng, không dựa vào timestamp.

### 3.2 Push/pull semantics

- Local entity mutation + outbox append cùng một SQLite transaction.
- Push batch có giới hạn count/bytes; trả result riêng cho từng operation. Không
  retry toàn batch nếu một item permanent-fail.
- Preserve operation order/dependency per plan; independent trips có thể sync
  song song.
- Pull page chụp `high_watermark`; pages dùng stable order đến watermark đó.
- Apply toàn page + advance cursor trong cùng local transaction. Apply fail thì
  cursor không đổi.
- Error taxonomy cố định: `retryable`, `conflict`, `auth_required`,
  `permission_revoked`, `client_upgrade_required`, `permanent_validation`.
- Backoff exponential + jitter; tôn trọng `Retry-After`; giữ nguyên operation ID
  và idempotency key qua mọi retry.

### 3.3 Conflict/deletion rules

- Expense/split/FX/settlement: không LWW; version conflict trả canonical snapshot
  + conflict metadata, resolve bằng new revision.
- Checklist/toggle: dùng intent command (`mark_done`, `mark_open`) hoặc version,
  tránh offline boolean overwrite.
- Poll vote: upsert theo voter + option/poll rule với unique constraint.
- Ordering: fractional key + deterministic rebalance operation.
- Delete-vs-edit: tombstone wins; user có thể recreate/copy, không hồi sinh ID cũ.
- Financial “delete” luôn là void/reversal, tombstone chỉ áp dụng representation.
- Tombstone retained ít nhất 180 ngày nếu support offline 90 ngày.
- Device có cursor cũ hơn compaction floor nhận `410 FULL_RESYNC_REQUIRED`.
- Removed member mất access ngay trên server; lần sync kế tiếp phải purge plan
  keys/cache/local data theo policy. Push notification không phải security control.

### 3.4 Schema evolution

- Expand server schema → deploy server backward-compatible → client migration →
  observe adoption → contract/cleanup.
- Sync handshake truyền app/schema/command versions và compaction floor.
- Server hỗ trợ một cửa sổ version được ghi rõ; client quá cũ có thể pull/export
  nhưng bị chặn write nếu không còn bảo toàn invariant.
- Pending outbox phải được migrate hoặc quarantine với user-visible recovery;
  không silently drop.

## 4. Authentication, authorization và invite security

### 4.1 Authentication/session

- Buy managed OIDC/auth. Mobile dùng Authorization Code + PKCE theo OAuth Security
  BCP; không dùng implicit/password grant.
- Access token ngắn hạn; rotating refresh token với reuse detection; secure
  Keychain/Keystore; device/session list + remote revoke.
- Không nhét plan membership/role dài hạn vào JWT rồi tin đến hết hạn. Mỗi request
  kiểm tra current membership/state; cache rất ngắn và có invalidation.
- Step-up auth cho account deletion, owner transfer, export toàn bộ, xem/download
  tài liệu nhạy cảm và thay đổi security settings.

### 4.2 Authorization model

- Deny by default; central policy function/middleware cho mọi endpoint và signed
  URL issuance.
- Permission matrix phải cover role (`owner/admin/member/viewer`), membership
  state (`invited/pending/active/left/removed`), object visibility, object owner,
  action và plan state.
- RLS là defense in depth, không thay application authorization. `FORCE ROW LEVEL
  SECURITY` ở tables phù hợp; production API role không là table owner và không
  có `BYPASSRLS`.
- Test riêng service/admin/background roles vì PostgreSQL table owner/superuser có
  thể bypass RLS.
- Object IDs đoán được cũng không thay authorization; mọi query scope bằng
  `plan_id` + current membership.
- Owner transfer atomic; plan luôn còn ít nhất một owner hoặc đi qua recovery
  workflow. Removed member giữ display snapshot trong lịch sử nhưng không access.

### 4.3 Invite flow

- Token random >=128 bits; khuyến nghị 32 random bytes base64url; DB chỉ lưu hash.
- Single/limited use, expiry ngắn (đề xuất 7 ngày), revoke/rotate, optional intended
  email/domain và approval.
- Redemption transaction lock invite row, re-check expiry/revoke/use count, create
  membership, increment use count và audit atomically.
- Public preview chỉ allowlist name/cover/date coarse nếu organizer cho phép; tuyệt
  đối không lộ members, exact itinerary, balances, booking data.
- Error/timing không cho enumerate plan/account. Rate limit theo IP + token hash +
  device/account; adaptive bot challenge cho abuse.
- Guest-claim là atomic identity merge có audit; không cho cùng guest identity
  claim bởi hai account hoặc invite email/link tạo duplicate member.

## 5. Privacy và media pipeline

### 5.1 Data classes

- Restricted: auth secrets, invite tokens, booking confirmation codes, exports.
- Sensitive: exact itinerary/location, receipts, balances, member contacts.
- Internal: audit metadata, provider IDs.
- Public-by-explicit-choice: recap allowlist fields only.

Booking codes/secret notes nên envelope-encrypt bằng KMS-managed key; search/index
không dùng plaintext. Không khuyến khích passport/identity documents; nếu chưa có
legal/security controls thì reject loại upload này thay vì “best effort store”.

### 5.2 Upload state machine

```text
initiated -> uploaded -> quarantined -> scanning -> ready
                                      -> rejected
ready -> deleting -> deleted
```

- Client upload trực tiếp vào quarantine bằng short-lived signed URL.
- Random storage key; original filename chỉ metadata đã sanitize.
- Extension allowlist + MIME + magic bytes; size/page/pixel/decompression limits;
  malware scan; image re-encode; EXIF/GPS strip mặc định; PDF active-content policy.
- Chỉ `ready` attachment được download/share/link vào recap.
- Signed download URLs short-lived (đề xuất 5 phút), issued sau authorization;
  không cache public cho private objects.
- Checksums, per-user/plan quotas, resumable upload, orphan cleanup, derivative
  cleanup và CDN invalidation.
- Provider backup caveat: Supabase database backup chỉ có Storage metadata, không
  có object bytes. Bắt buộc object versioning/replication/backup + manifest.

### 5.3 Lifecycle/privacy

- Không log raw request body, receipt OCR, description, confirmation code, token,
  exact balance hoặc signed URL.
- Analytics dùng coarse buckets/IDs pseudonymous; financial authoritative events
  phát từ server.
- Account/plan deletion là durable workflow theo manifest; xóa originals,
  derivatives, exports, tokens và search/cache copies; audit outcome.
- Retention schedule riêng cho app data, audit, logs, idempotency, tombstones,
  backups và rejected uploads.
- Nếu restore backup, re-apply deletion ledger để không resurrect dữ liệu đã được
  yêu cầu xóa trong thời gian backup retention.

## 6. Notifications

- Ghi domain event + `notification_outbox` trong cùng DB transaction. Worker đọc
  outbox theo at-least-once; không gửi trực tiếp trong request transaction.
- Dedup unique key: `(domain_event_id, recipient_id, channel, template_version)`.
- Trước delivery, re-check membership, object visibility, preference/consent và
  plan state; event cũ không được leak data cho removed member.
- Security/transactional, actionable reminder, digest và marketing là các class
  riêng. Marketing có consent riêng; reminder/digest có quiet hours/timezone.
- Notification lock-screen body tối thiểu, không chứa amount, booking code,
  location/hotel hoặc private caption mặc định.
- Push token per device, encrypted at rest; handle APNs/FCM invalid-token feedback;
  logout/revoke unregister token.
- Retry bounded + jitter, provider circuit breaker, DLQ, replay tool giữ dedup key.
- Metrics: outbox age, attempted/sent/provider-accepted/delivered-if-supported,
  opened/actioned, bounce/invalid token, suppression reason và duplicate blocked.

## 7. Observability và safe degradation

### 7.1 Telemetry

OpenTelemetry traces/metrics/structured logs với `request_id`, `trace_id`,
`operation_id`, `device_id_hash`, `release`, `schema_version`; plan/user IDs nên
hash/pseudonymize trong telemetry ngoài audit store.

Critical metrics/alerts:

- ledger invariant violation (target 0), projection drift (target 0), ledger
  reconciliation lag;
- idempotency replay/collision, serialization retry/deadlock, financial error rate;
- outbox oldest age, retry counts, permanent failures, conflict rate, sync lag,
  full-resync rate;
- auth failure/deny spikes, invite enumeration/abuse, active signed URL failures;
- upload quarantine/scan backlog và malware rejection;
- notification outbox/DLQ age;
- DB connections/locks/replica lag/storage, job queue lag;
- backup age, last successful restore drill, measured RPO/RTO.

Trace 100% error và financial mutation paths nếu chi phí cho phép; sample reads.
Audit store tách operational logs, append-only/tamper-evident và access-controlled.

### 7.2 Safe degradation

- Projection drift: không phục vụ con số nghi sai; rebuild/read canonical ledger,
  mark degraded và alert.
- FX provider down: dùng cached rate marked stale hoặc require manual rate; không
  silently fabricate.
- Notification/provider down: core write vẫn commit, outbox chờ retry.
- Media scanner down: object ở quarantine, core app vẫn chạy.
- DB under stress: ưu tiên authenticated reads/financial integrity, throttle
  exports/media/recompute; kill switch có thể chuyển financial mutations read-only.

## 8. Test strategy

### 8.1 Required suites

- Unit/golden: every currency exponent, split methods, remainder ties, FX direction,
  refunds, reversals, fund flows, amount boundaries.
- Property-based/state-machine: random sequence of create/edit/void/refund/settle/
  reverse/fund actions; verify all ledger invariants after every step.
- Mutation testing cho split/rounding/settlement engine để chứng minh tests bắt được
  off-by-one và sign inversion.
- Integration trên real PostgreSQL/Testcontainers: constraints, transaction abort,
  serializable retry, RLS/policy matrix, projection rebuild, idempotency replay.
- Concurrency: many writers to same plan/entity, duplicate operation IDs, delayed
  commits và deadlocks.
- Model-based multi-device sync: duplicate/drop/reorder request/response/change,
  stale cursor, 30/90-day offline, delete-vs-edit, schema upgrade with pending outbox.
- Security: horizontal/vertical IDOR across every resource/action, revoked/removed
  sessions, invite brute force, signed URL, upload polyglot/decompression bomb,
  log redaction, service-role/RLS bypass regression.
- Migration: upgrade from every supported server/local schema, expand-contract,
  rollback application binary, pending outbox and tombstone preservation.
- Load: expected peak ×2 plus 100-member/10k-expense stress fixture; long outbox,
  balance rebuild, exports and media upload isolated from core API.

### 8.2 Chaos/fault injection

- kill API after DB commit before HTTP response;
- kill worker after provider accepted notification but before ack;
- lose/duplicate/reorder queue events and sync pages;
- DB failover/connection exhaustion/serialization storm;
- Redis/cache, FX, email, push, storage và scanner outages;
- object upload succeeds but finalize fails; delete metadata but object call times out;
- clock skew/DST/device clock wrong;
- corrupt projection and rebuild from ledger;
- restore DB to isolated environment while object store is at different point.

No test may rely on arbitrary `sleep`; use deterministic clocks, seeded generators
và controllable fault points.

## 9. Backup và disaster recovery

### 9.1 Proposed public-launch objectives

- Financial/core PostgreSQL: RPO <= 5 minutes, RTO <= 4 hours.
- Object/media: RPO <= 1 hour, RTO <= 24 hours; disclose if lower durability is
  intentionally accepted.
- Auth/config/keys/IaC: reproducible or backed up separately; key loss must not make
  encrypted fields permanently unreadable.

### 9.2 Controls

- Managed PITR with >=14-day window for production DB.
- Daily encrypted logical export to separate account/region: 30 daily, 12 weekly,
  12 monthly copies (adjust after legal/cost review).
- Object versioning + lifecycle + independent replication/backup; daily manifest
  of storage key/checksum/owner.
- Backup encryption keys/secrets separated from backup payload; least-privilege
  restore role; destructive restore requires two-person procedure when team permits.
- IaC, migrations, auth config, RLS policies, queues/schedules and notification
  templates included in recovery runbook.
- Monthly automated restore into isolated environment; quarterly full game day.
  Verify row counts, constraints, ledger invariants, projection rebuild, RLS tests,
  sample object checksums và actual RTO/RPO.
- Never declare restore complete until app smoke tests and reconciliation pass.

## 10. Rate limits/abuse controls

Use layered token bucket/sliding windows at edge + app, keyed by combinations of
IP, account, device, plan, invite-token hash và endpoint. Starting values are
hypotheses, load-test/tune before launch:

| Surface | Starting guardrail |
|---|---:|
| Login/passwordless request | 5/account/hour; 20/IP/hour |
| Invite preview | 60/IP/minute; bot challenge on anomaly |
| Invite redemption | 5/token/hour; 10/IP/hour |
| Invite creation | 20/plan/day |
| Financial mutation | burst 30/min/user, 120/min/plan |
| Sync push | 60 requests/min/device; max 100 ops or 1 MiB/batch |
| Sync pull | 120/min/device with cursor/caching |
| Upload | 5 concurrent/account; quota by bytes/day/plan |
| Export/recap generation | 3/hour/user, 10/day/plan |
| Reminder-triggering action | 10/hour/actor/plan + recipient dedup |

- Return `429` + `Retry-After`; queued offline operation không bị drop/renumber.
- NAT/shared-IP false positives: authenticated limits ưu tiên account/device/plan,
  IP chỉ là abuse signal.
- Separate expensive compute/storage quotas from normal API quota.
- Hard body/page/batch/file limits trước parsing/processing.
- Alert on limit saturation and top offenders; provide support override/audit.

## 11. Integrated quality gates (not product versions)

### Gate A — Architecture/invariants approved

- ADRs cho ledger signs, fund semantics, FX direction/rounding, conflict matrix,
  tombstone/idempotency retention, permission matrix, data classification, RPO/RTO.
- Threat model và abuse cases reviewed.
- OpenAPI + sync/error contracts executable; no ambiguous money field.

### Gate B — Financial truth proven

- All hard invariants enforced in code/DB and documented.
- Golden + property/state-machine + concurrency suites green with fixed seeds and
  overnight randomized corpus.
- Kill-after-commit replay produces exactly one transaction.
- Projection corrupt/rebuild test produces byte-for-byte equivalent balances.
- No direct production role can mutate ledger tables outside service path.

### Gate C — Sync convergence proven

- Fault matrix (drop/duplicate/reorder/lost ack) converges without data loss.
- 90-day offline + tombstone/full-resync + pending-outbox migration green.
- Financial conflicts never auto-LWW; permission revoke purges access/local data.
- Sync SLO measured under weak-network/load fixture.

### Gate D — Security/privacy/media proven

- 100% permission-matrix tests and cross-plan IDOR suite green.
- No open critical/high findings in dependency/SAST/DAST/manual threat review.
- Invite enumeration/brute force controls verified.
- Upload quarantine/scan/EXIF/deletion/backup behavior tested end-to-end.
- Logs, traces, analytics and support exports pass sensitive-data redaction tests.

### Gate E — Operations/DR proven

- Dashboards/alerts emit test signals; runbooks exercised.
- Latest isolated restore drill meets RPO/RTO, ledger reconciliation and object
  checksum checks.
- Provider outages degrade safely; DLQs/replay/kill switches work.
- Canary/rollback including backward-compatible DB migration rehearsed.

### Gate F — Public launch readiness

- Entire unified scope passes E2E and acceptance criteria; feature flags are only
  rollout/kill switches, not missing modules.
- Load test at >=2× forecast peak passes agreed SLO/error budgets.
- At least 14-day production-like soak: zero data-loss/duplicate-ledger incidents,
  zero unresolved invariant violations, no P0/P1 issue.
- Support, incident, privacy request, data export/deletion and status communication
  runbooks staffed and tested.

## 12. Key source notes

- PostgreSQL Serializable guarantees serial-equivalent committed results but the
  application must retry serialization failures:
  https://www.postgresql.org/docs/current/transaction-iso.html
- PostgreSQL RLS defaults deny when enabled without policy, but table owners and
  `BYPASSRLS` roles can bypass it:
  https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- OWASP recommends deny-by-default, checking permission on every request, secure
  single-use expiring URL tokens, and defense-in-depth upload validation:
  https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
  https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
  https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- OAuth Security Best Current Practice (RFC 9700):
  https://datatracker.ietf.org/doc/html/rfc9700
- NIST SP 800-63B-4 authentication/session guidance:
  https://pages.nist.gov/800-63-4/sp800-63b.html
- OpenTelemetry conventions/correlation:
  https://opentelemetry.io/docs/specs/semconv/general/
  https://opentelemetry.io/docs/specs/otel/logs/
- AWS Well-Architected requires defined RPO/RTO and periodic verified recovery:
  https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/back-up-data.html
- Supabase backup caveat: database backups do not contain Storage object bytes:
  https://supabase.com/docs/guides/platform/backups
- HTTP `Retry-After` semantics:
  https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after
