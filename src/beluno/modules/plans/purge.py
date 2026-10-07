"""Purge plans whose restore window has passed (worker role).

A plan scheduled for deletion can be restored for ``plan_purge_after_days``; after
that the SQL gate ``plans.purge_deleted_plan`` deletes it with everything it holds,
one plan per transaction so no lock is held for long. Audit events stay.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from beluno.modules.context import Runtime

PURGE_PLAN_SQL = text("SELECT plans.purge_deleted_plan(:cutoff)")
MAX_PLANS_PER_RUN = 200


async def purge_deleted_plans(runtime: Runtime) -> int:
    """Purge plans scheduled for deletion before the cutoff, one committed plan at a time."""

    cutoff = runtime.clock() - timedelta(days=runtime.settings.plan_purge_after_days)
    purged = 0
    for _ in range(MAX_PLANS_PER_RUN):
        async with runtime.database.transaction() as session:
            plan_id = (await session.execute(PURGE_PLAN_SQL, {"cutoff": cutoff})).scalar_one()
        if plan_id is None:
            break
        purged += 1
    return purged
