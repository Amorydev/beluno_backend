"""Procrastinate task registry. Handlers are idempotent and take identifiers only."""

from __future__ import annotations

from uuid import UUID

import procrastinate

from beluno.modules.iam.email_challenges import DELIVER_TASK_NAME, EMAIL_QUEUE, deliver_challenge
from beluno.modules.iam.maintenance import purge_expired_auth_records
from beluno.modules.plans.series import extend_all_series
from beluno.worker.runtime import get_email_sender, get_worker_runtime

app = procrastinate.App(connector=procrastinate.PsycopgConnector())

WORKER_QUEUES = ["maintenance", EMAIL_QUEUE, "plans"]


@app.task(queue="maintenance", retry=3, queueing_lock="maintenance:heartbeat")
async def heartbeat(release: str, payload_version: int = 1) -> str:
    """Synthetic idempotent job with an explicit payload-version contract."""

    if payload_version != 1:
        raise ValueError(f"Unsupported heartbeat payload version: {payload_version}")
    return release


@app.task(
    name=DELIVER_TASK_NAME,
    queue=EMAIL_QUEUE,
    retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=2),
)
async def deliver_email_challenge(challenge_id: str, payload_version: int = 1) -> bool:
    if payload_version != 1:
        raise ValueError(f"Unsupported email challenge payload version: {payload_version}")
    return await deliver_challenge(get_worker_runtime(), UUID(challenge_id), get_email_sender())


@app.periodic(cron="17 * * * *", periodic_id="iam.purge_expired_auth_records")
@app.task(name="iam.purge_expired_auth_records", queue="maintenance", retry=3)
async def purge_expired_auth(timestamp: int) -> int:
    del timestamp
    return await purge_expired_auth_records(get_worker_runtime())


@app.periodic(cron="5 * * * *", periodic_id="plans.extend_series_horizons")
@app.task(
    name="plans.extend_series_horizons",
    queue="plans",
    retry=3,
    queueing_lock="plans:extend_series_horizons",
)
async def extend_series_horizons(timestamp: int) -> int:
    """Hourly: materialize newly-in-horizon occurrences; idempotent by occurrence key."""

    del timestamp
    return await extend_all_series(get_worker_runtime())
