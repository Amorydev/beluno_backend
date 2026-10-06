# Sync Protocol

The client outbox command envelope contains an operation ID, idempotency key,
device ID, plan ID, command schema version, expected entity version, dependency
IDs, payload, and client timestamp.

The server change envelope uses monotonically ordered `server_seq`; device time
is metadata only. Pull pages use a fixed high watermark, and the client applies
one page plus its cursor in one local transaction.
