"""Retention jobs for the sync kernel (worker role).

Change rows older than the retention window are removed in batches and each
affected scope's compaction floor rises, so cursors that predate the floor must
resync. Expired operation records are purged the same way. The SQL gates refuse
cutoffs inside the supported offline window, so a misconfiguration cannot strand
devices that are still within it.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from beluno.modules.context import Runtime
from beluno.observability.metrics import instruments

COMPACT_SQL = text("SELECT sync_audit.compact_changes(:cutoff, :batch)")
PURGE_SQL = text("SELECT sync_audit.purge_operations(:now, :batch)")
BATCH_SIZE = 5_000
MAX_BATCHES_PER_RUN = 200


async def compact_changes(runtime: Runtime) -> int:
    """Remove change rows past retention, one committed batch at a time."""

    cutoff = runtime.clock() - timedelta(days=runtime.settings.sync_change_retention_days)
    removed = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        async with runtime.database.transaction() as session:
            batch = (
                await session.execute(COMPACT_SQL, {"cutoff": cutoff, "batch": BATCH_SIZE})
            ).scalar_one()
        if not batch:
            break
        removed += int(batch)
        instruments().changes_compacted.add(int(batch))
    return removed


async def purge_operations(runtime: Runtime) -> int:
    """Remove operation records whose retention has elapsed."""

    removed = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        async with runtime.database.transaction() as session:
            batch = (
                await session.execute(PURGE_SQL, {"now": runtime.clock(), "batch": BATCH_SIZE})
            ).scalar_one()
        if not batch:
            break
        removed += int(batch)
        instruments().operations_purged.add(int(batch))
    return removed
