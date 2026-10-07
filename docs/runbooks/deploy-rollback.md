# Deploy and Rollback Runbook

## Artifacts

One container image (`Dockerfile`) runs every process. The command picks which:

| Process | Command | Database role | `BELUNO_PROCESS_ROLE` |
|---|---|---|---|
| API (default) | `uvicorn beluno.api.main:app --proxy-headers` | `api_runtime` | `api` |
| Worker (jobs and periodic jobs) | `python -m beluno.worker.main` | `worker_runtime` | `worker` |
| Scheduler (one-shot heartbeat) | `python -m beluno.scheduler.main` | `scheduler_runtime` | `scheduler` |
| Migrate (one-shot) | `python scripts/bootstrap_database.py` | `migrator` | `migrate` |

In staging and production each process gets only its own database URL. Set
`BELUNO_PROCESS_ROLE` so start-up checks ask for nothing else. The default `all`
requires every URL.

`deploy/staging/` holds a compose stack that runs on any host with Docker:
PostgreSQL, migrate, API, worker, OTel Collector, Prometheus with the alert
rules, Alertmanager, and Grafana with the overview dashboard.

```bash
cp deploy/staging/env.example deploy/staging/.env
uv run python scripts/generate_signing_key.py --with-token-hash-key
docker compose -f deploy/staging/compose.yaml up -d --build
```

1. Replace every `CHANGE_ME` in `.env`.
2. Paste the keys that `generate_signing_key.py` prints into `.env`.

Ports bind to 127.0.0.1 by default (`BELUNO_PUBLISH_ADDRESS` for the API,
`BELUNO_MONITORING_PUBLISH_ADDRESS` for Grafana, Prometheus, and Alertmanager; the
last two have no authentication). Put a TLS reverse proxy in front of the API and
set `FORWARDED_ALLOW_IPS` to the address it connects from: through Docker's
bridge that is usually the gateway, not 127.0.0.1. Point Alertmanager's receiver
at a real channel before relying on alerts.

Migrate and the scheduler need only their database URL; the API and the worker
also need the signing keys, the token hash key, and SMTP in staging and
production.

## Deploy

1. Check the release artifact, the migration review (header: forward action, lock
   risk, validation, compatibility, rollback), and the environment configuration.
2. Take a fresh backup (`scripts/db_drill.py`, see `database-drills.md`).
3. Run the migrate step. Migrations must be backward compatible with the running
   release.
4. Deploy a canary API and the worker.
5. Compare these against the current release on the Grafana overview:
   - error rate
   - sync push results and pull pages
   - job queue age
   - ledger lock wait
   - command p95 against the targets in `performance.md`
6. Roll out, or abort.

## Rollback

1. Redeploy the previous image. Expanded schemas stay compatible with it, and
   migrations are forward-only, so never downgrade the database.
2. If a feature misbehaves without a code rollback, turn it off with a kill
   switch (below).
3. When data is damaged, restore per `database-drills.md`. That procedure bumps
   sync generations, so devices resync.
4. Never truncate ledger, audit, change-log, idempotency, or tombstone data as a
   rollback step.

**Rehearsal:** on the staging stack:

1. Deploy release N.
2. Seed it (`scripts/load_seed.py`).
3. Deploy N+1 with its migration.
4. Redeploy N's image.
5. Check `/health/ready`, a sync pull, and the reconciliation job.

Time each step and record it next to the drill timings.

## Booking keys

`BELUNO_BOOKING_KEYS` (API only) seals booking codes and private notes. To rotate,
add a new key to the keyring, make it `active`, and redeploy; every booking saved
afterwards is sealed with it, and old keys must stay until no row uses them
(`SELECT key_id, count(*) FROM bookings.booking_secrets GROUP BY 1`). Losing a key
loses those secrets; back the keyring up with the database credentials, never in
the repository.

## Passkey relying party

Passkeys need the app's domain as `BELUNO_WEBAUTHN_RP_ID` and every origin a passkey
response comes from in `BELUNO_WEBAUTHN_ORIGINS` (JSON list): `https://` web origins
and `android:apk-key-hash:<base64url SHA-256 of the signing certificate>` for the
Android app. iOS needs the domain in the app's associated domains
(`webcredentials:`) and `/.well-known/apple-app-site-association` served there;
Android needs `/.well-known/assetlinks.json`. Staging and production refuse
`localhost` and plain `http`. Changing the RP ID orphans every existing passkey:
people then sign in another way and add a new one.

## Media storage and scanning

Media lives in RustFS (`rustfs` service, S3 API on 9000, data in the `rustfs-data`
volume) and every upload is scanned by ClamAV (`clamav` service, signatures in
`clamav-data`, refreshed by its own freshclam; it needs about 2 GB of memory and a
few minutes after first start). The worker creates the bucket on start. Apps reach
storage only through presigned URLs signed for `BELUNO_STORAGE_PUBLIC_URL`: publish
RustFS behind the TLS proxy at that address (and allow the app's origin for CORS
if the web client uploads). While ClamAV is down, uploads stay `scanning` and the
`media.process` job retries with backoff; nothing unscanned is ever served. Media jobs
run on their own `media` queue with the worker's concurrency of 4, so scans never
hold up sign-in email; a file stuck scanning for an hour is re-queued by the hourly
`media.sweep`. Pin `RUSTFS_VERSION` and `CLAMAV_VERSION` in `.env`. clamd's default
`StreamMaxLength` (25M) covers the media limits (20 MB images, 15 MB receipts):
raise it in clamd.conf before raising `BELUNO_MEDIA_*_MAX_BYTES`, or larger files
are rejected as too large. The bundled stack uses RustFS's root keys as the app's
keys; in production create a bucket-scoped access key for the API and worker
instead. Back up
the `rustfs-data` volume with the database: a restored database pointing at missing
objects shows files that cannot be downloaded.

## Push notifications

Set `BELUNO_FCM_SERVICE_ACCOUNT_JSON` (worker only) to a Firebase service account
JSON with the Cloud Messaging role; without it notifications are recorded and
marked skipped. The iOS app needs its APNs key uploaded to the Firebase project.
`notifications.dispatch` runs every minute on the `media` queue, never two at once:
it fans out the events queued in `engagement.fan_out_queue`, queues reminders, and
delivers a batch with one `send_each` call (10-second HTTP timeout); FCM outages and
credential problems back off up to five attempts, then fail; tokens FCM no longer
accepts are deleted.

## Paid plans

The API verifies purchases; the worker confirms Google ones (`billing.acknowledge_purchase`,
retried for about a day; Google refunds purchases left unconfirmed for three days).

- **App Store:** set `BELUNO_APPLE_BUNDLE_ID`, `BELUNO_APPLE_APP_APPLE_ID` (required in
  Production), `BELUNO_APPLE_ENVIRONMENT`, and `BELUNO_APPLE_ROOT_CERTIFICATES` (paths to
  Apple's root certificates, DER, from apple.com/certificateauthority; staging mounts
  `BELUNO_APPLE_CERTIFICATES_DIR` at `/etc/beluno/apple`). Point App Store Server
  Notifications (version 2) at `/v1/store-notifications/apple`. Verification checks
  certificate revocation online (OCSP); keep outbound HTTPS open from the API.
- **Google Play:** set `BELUNO_GOOGLE_PLAY_PACKAGE_NAME` and
  `BELUNO_GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` (a service account invited in Play Console
  with financial data access). For real-time developer notifications, create a Pub/Sub
  push subscription to `/v1/store-notifications/google` with authentication on, and set
  `BELUNO_GOOGLE_PLAY_PUSH_AUDIENCE` and `BELUNO_GOOGLE_PLAY_PUSH_SERVICE_ACCOUNT` to
  its audience and service account.
- **Limits and products:** `BELUNO_FREE_ACTIVE_TRIPS`, `BELUNO_MEDIA_RECEIPTS_PER_PLAN`,
  `BELUNO_STORE_TRIP_PASS_PRODUCT_IDS`, `BELUNO_STORE_PRO_PRODUCT_IDS` (JSON lists).
  Unset limits mean no limit; unset stores answer `503 STORE_UNAVAILABLE`.

## Kill switches

Flip one in the environment and restart the affected process. Clients keep their
queues: push answers `retry` for refused commands.

| Setting | Effect |
|---|---|
| `BELUNO_FINANCE_WRITES_ENABLED=false` | Every finance command answers `503 FEATURE_DISABLED`; reads, explanations, and pull stay up (see `finance-operations.md`) |
| `BELUNO_SYNC_PUSH_ENABLED=false` | `/v1/sync/push` answers `503`; clients keep their queues |
| `BELUNO_SYNC_PULL_ENABLED=false` | `/v1/sync/pull` answers `503`; the handshake still reports it |
| `BELUNO_SYNC_DISABLED_COMMANDS='["plan.duplicate"]'` | Named commands are refused on REST (`503`) and push (`retry`) alike |
| `BELUNO_INVITES_ENABLED=false` | Invite links stop previewing and redeeming |
| `BELUNO_GUEST_ACCESS_ENABLED=false` | Links stop minting guest accounts; signed-in people can still join |
| `BELUNO_PARTICIPANT_CLAIMS_ENABLED=false` | Placeholder claim links stop being issued and redeemed |

Disable a command rather than deleting accepted operations or change rows.

## Release 1 surface

These jobs run in the worker, in the `maintenance` queue:

| Job | When | What it does |
|---|---|---|
| `iam.purge_expired_auth_records` | hourly | Expired challenges, tokens, and rate-limit windows; sessions that ended more than 30 days ago |
| `sync.compact_changes`, `sync.purge_operations`, `activity.purge_events` | daily | Retention per `docs/contracts/retention-matrix.md` |
| `decisions.close_due_polls` | every 5 min | Closes polls past their deadline (one result each, even racing an organiser) |
| `plans.purge_deleted` | daily | Deletes plans whose deletion was scheduled `BELUNO_PLAN_PURGE_AFTER_DAYS` (30) ago |
| `finance.reconcile_ledgers` | daily | Ledger drift check (`finance-operations.md`) |
| `jobs.report_queue_health` | every 5 min | Queue depth and oldest waiting age for the alerts |

- **Account deletion** (`DELETE /v1/me`) is immediate. A plan its owner had alone
  is scheduled for deletion and purged with the others.
- **Restore requests** for a purged plan cannot be met. Before the purge, the
  owner restores the plan in the app.
- **Alerts** live in `deploy/staging/prometheus/rules/beluno-alerts.yml`:
  - `LedgerDrift` → `finance-operations.md` "Drift response";
  - queue alerts → `sync-operations.md` "Dead letters";
  - latency alerts → `performance.md`.

## Proxy and client addresses

Abuse limits for unauthenticated endpoints key on the client address. Behind a
load balancer run uvicorn with `--proxy-headers --forwarded-allow-ips=<LB CIDRs>`
(the image reads `FORWARDED_ALLOW_IPS`; never `*` on a publicly reachable port).
Otherwise every caller shares the proxy's address and one rate-limit bucket.
