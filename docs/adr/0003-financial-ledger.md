# ADR 0003: Financial Ledger

**Status:** Accepted

Financial history is append-only. Expenses have immutable revisions; changes,
voids, refunds, settlements, and fund activity create balanced postings rather
than overwriting history. Projections are rebuildable caches.
