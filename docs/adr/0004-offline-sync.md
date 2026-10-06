# ADR 0004: Offline Sync

**Status:** Accepted

Clients persist local changes and an outbox atomically. The server accepts
idempotent commands and exposes cursor-based changes from an append-only change
log. Realtime is only a wake-up hint; financial conflicts never use blind
last-write-wins.
