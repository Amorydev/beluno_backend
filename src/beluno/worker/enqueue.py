"""Enqueue Procrastinate jobs inside the caller's domain transaction.

The job row commits or rolls back together with the state change that needs it,
which makes the queue a transactional outbox. Arguments must carry identifiers
only, never secrets or contact data. The defer function version is pinned with
the locked Procrastinate release and covered by the live PostgreSQL suite.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFER_SQL = text(
    """
    SELECT jobs.procrastinate_defer_jobs_v1(ARRAY[
        ROW(:queue, :task_name, 0, NULL, :queueing_lock, CAST(:args AS jsonb), NULL)
        ::jobs.procrastinate_job_to_defer_v1
    ])
    """
)


async def defer_in_transaction(
    session: AsyncSession,
    *,
    task_name: str,
    queue: str,
    args: Mapping[str, str | int],
    queueing_lock: str | None = None,
) -> None:
    previous_search_path = (
        await session.execute(text("SELECT current_setting('search_path')"))
    ).scalar_one()
    # The defer function resolves Procrastinate tables through search_path. A
    # failure aborts the whole transaction, which also discards this local setting.
    await session.execute(text("SELECT set_config('search_path', 'jobs, public', true)"))
    await session.execute(
        DEFER_SQL,
        {
            "queue": queue,
            "task_name": task_name,
            "queueing_lock": queueing_lock,
            "args": json.dumps(dict(args), sort_keys=True),
        },
    )
    await session.execute(
        text("SELECT set_config('search_path', :path, true)"),
        {"path": previous_search_path},
    )
