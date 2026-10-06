"""Enqueue Procrastinate jobs inside the caller's domain transaction.

The job row commits or rolls back together with the state change that needs it,
which makes the queue a transactional outbox. Arguments must carry identifiers
only, never secrets or contact data. The defer function version is pinned with
the locked Procrastinate release and covered by the live PostgreSQL suite.

A ``queueing_lock`` makes the enqueue duplicate-safe: while a job with that lock
is still waiting, enqueueing it again is a no-op instead of a second job.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

DEFER_SQL = text(
    """
    SELECT jobs.procrastinate_defer_jobs_v1(ARRAY[
        ROW(:queue, :task_name, 0, NULL, :queueing_lock, CAST(:args AS jsonb), NULL)
        ::jobs.procrastinate_job_to_defer_v1
    ])
    """
)
QUEUEING_LOCK_INDEX = "procrastinate_jobs_queueing_lock_idx_v1"


async def defer_in_transaction(
    session: AsyncSession,
    *,
    task_name: str,
    queue: str,
    args: Mapping[str, str | int],
    queueing_lock: str | None = None,
) -> bool:
    """Queue one job; returns False when an identical job is already waiting."""

    previous_search_path = (
        await session.execute(text("SELECT current_setting('search_path')"))
    ).scalar_one()
    # The defer function resolves Procrastinate tables through search_path. A
    # failure aborts the whole transaction, which also discards this local setting.
    await session.execute(text("SELECT set_config('search_path', 'jobs, public', true)"))
    parameters = {
        "queue": queue,
        "task_name": task_name,
        "queueing_lock": queueing_lock,
        "args": json.dumps(dict(args), sort_keys=True),
    }
    queued = True
    try:
        async with session.begin_nested():
            await session.execute(DEFER_SQL, parameters)
    except IntegrityError as error:
        if queueing_lock is None or QUEUEING_LOCK_INDEX not in str(error.orig):
            raise
        queued = False
    await session.execute(
        text("SELECT set_config('search_path', :path, true)"),
        {"path": previous_search_path},
    )
    return queued
