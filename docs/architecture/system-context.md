# System Context

Beluno is a Python modular monolith with one PostgreSQL write region.

```text
Mobile / web clients
  └─ Self-issued ES256 JWT ──> FastAPI API (identity provider, ADR 0007)
                                  ├─ PostgreSQL (domain, audit, RLS)
                                  ├─ S3-compatible storage (media phase)
                                  └─ transactional outbox ──> Procrastinate workers
```

Clients never write domain tables directly. The API owns authorization,
idempotency, audit records, change-log entries, and domain transactions. Offline
clients talk to `/v1/sync/handshake`, `/v1/sync/push`, and `/v1/sync/pull`
(`docs/contracts/sync-protocol.md`); REST mutations are the same commands.

The application runs in three concurrent processes from one package:

- **API:** HTTP requests, synchronous domain commands, and identity provisioning.
- **Worker:** idempotent background consumers (email delivery, invite processing, etc.).
- **Scheduler:** creates named periodic jobs and state-machine transitions.
