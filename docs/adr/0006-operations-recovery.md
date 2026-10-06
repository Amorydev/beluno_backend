# ADR 0006: Operations and Recovery

**Status:** Accepted

Target public-launch objectives are database RPO ≤5 minutes/RTO ≤4 hours and
media RPO ≤1 hour/RTO ≤24 hours. Database and object backups are separate.
Recovery is accepted only after an isolated restore verifies authorization,
ledger invariants, projections, and object checksums.
