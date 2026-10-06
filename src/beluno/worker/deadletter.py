"""Operator tooling for the job queue (worker role): dead letters, retries, health.

Procrastinate keeps a job that exhausted its retries in status ``failed``; that
is the dead-letter queue. Every replay is recorded as an audit event naming the
operator, and job arguments only ever contain identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from beluno.modules.context import Runtime, open_context
from beluno.modules.sync_audit.recorder import record_audit
from beluno.observability.metrics import report_queue_health

LIST_SQL = text(
    """
    SELECT id, task_name, queue_name, status::text, attempts, scheduled_at, args, queueing_lock
    FROM jobs.procrastinate_jobs
    WHERE status = CAST(:status AS jobs.procrastinate_job_status)
    ORDER BY id
    LIMIT :limit
    """
)
GET_SQL = text(
    """
    SELECT id, task_name, queue_name, status::text, attempts, scheduled_at, args, queueing_lock
    FROM jobs.procrastinate_jobs WHERE id = :id
    """
)
RETRY_SQL = text("SELECT jobs.procrastinate_retry_job_v2(:id, :retry_at, NULL, NULL, NULL)")
HEALTH_SQL = text(
    """
    SELECT
        count(*) FILTER (WHERE status = 'todo') AS todo,
        count(*) FILTER (WHERE status = 'doing') AS doing,
        count(*) FILTER (WHERE status = 'failed') AS failed,
        coalesce(
            extract(epoch FROM (
                now() - min(coalesce(scheduled_at, e.first_deferred)) FILTER (WHERE status = 'todo')
            )),
            0
        ) AS oldest_waiting_seconds
    FROM jobs.procrastinate_jobs AS j
    LEFT JOIN LATERAL (
        SELECT min(at) AS first_deferred FROM jobs.procrastinate_events WHERE job_id = j.id
    ) AS e ON true
    """
)


@dataclass(frozen=True)
class QueuedJob:
    id: int
    task_name: str
    queue_name: str
    status: str
    attempts: int
    scheduled_at: datetime | None
    args: dict[str, Any]
    queueing_lock: str | None


@dataclass(frozen=True)
class QueueHealth:
    todo: int
    doing: int
    failed: int
    oldest_waiting_seconds: float

    def as_measurements(self) -> dict[str, float]:
        return {
            "todo": float(self.todo),
            "doing": float(self.doing),
            "failed": float(self.failed),
            "oldest_waiting_seconds": float(self.oldest_waiting_seconds),
        }


def _job(row: Any) -> QueuedJob:
    return QueuedJob(
        id=row.id,
        task_name=row.task_name,
        queue_name=row.queue_name,
        status=row.status,
        attempts=row.attempts,
        scheduled_at=row.scheduled_at,
        args=dict(row.args or {}),
        queueing_lock=row.queueing_lock,
    )


async def list_jobs(
    runtime: Runtime, *, status: str = "failed", limit: int = 50
) -> list[QueuedJob]:
    async with runtime.database.transaction() as session:
        rows = await session.execute(LIST_SQL, {"status": status, "limit": limit})
        return [_job(row) for row in rows.all()]


async def retry_job(runtime: Runtime, job_id: int, *, operator: str) -> QueuedJob:
    """Put a failed job back on the queue now; the replay is audited."""

    async with open_context(runtime) as ctx:
        session = ctx.session
        row = (await session.execute(GET_SQL, {"id": job_id})).one_or_none()
        if row is None or row.status != "failed":
            raise LookupError(f"job {job_id} is not a failed job")
        # The retry function resolves Procrastinate tables through search_path.
        await session.execute(text("SELECT set_config('search_path', 'jobs, public', true)"))
        await session.execute(RETRY_SQL, {"id": job_id, "retry_at": ctx.now})
        await record_audit(
            ctx,
            action="job.retried",
            entity_type="job",
            entity_id=UUID(int=job_id),
            metadata={"job_id": job_id, "task_name": row.task_name, "operator": operator},
        )
        updated = (await session.execute(GET_SQL, {"id": job_id})).one()
        return _job(updated)


async def queue_health(runtime: Runtime) -> QueueHealth:
    async with runtime.database.transaction() as session:
        row = (await session.execute(HEALTH_SQL)).one()
    health = QueueHealth(
        todo=int(row.todo),
        doing=int(row.doing),
        failed=int(row.failed),
        oldest_waiting_seconds=float(row.oldest_waiting_seconds),
    )
    report_queue_health(health.as_measurements())
    return health
