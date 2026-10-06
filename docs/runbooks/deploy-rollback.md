# Deploy and Rollback Runbook

1. Verify the release artifact, migration review, and environment configuration.
2. Apply only backward-compatible migrations under the migrator role.
3. Deploy a canary API and named safe worker queues.
4. Compare error rate, authorization denials, sync lag, outbox age, and database
   locks against the current release.
5. Abort by disabling new commands/consumers or redeploying the previous artifact
   while the expanded schema remains compatible.
6. Never truncate ledger, audit, change-log, idempotency, or tombstone data as a
   rollback operation.

## Proxy and client addresses

Abuse limits for unauthenticated endpoints key on the client address. Behind a
load balancer run uvicorn with `--proxy-headers --forwarded-allow-ips=<LB CIDRs>`
(never `*` on a publicly reachable port); otherwise every caller shares the
proxy's address and one rate-limit bucket.
