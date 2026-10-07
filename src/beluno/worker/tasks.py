"""Procrastinate task registry. Handlers are idempotent and take identifiers only."""

from __future__ import annotations

from uuid import UUID

import procrastinate

from beluno.modules.finance.maintenance import reconcile_ledgers
from beluno.modules.finance.market_rates import ingest_market_rates, rate_provider
from beluno.modules.iam.email_challenges import DELIVER_TASK_NAME, EMAIL_QUEUE, deliver_challenge
from beluno.modules.iam.maintenance import purge_expired_auth_records
from beluno.modules.sync_audit.maintenance import (
    compact_changes,
    purge_activity,
    purge_operations,
)
from beluno.worker.deadletter import queue_health
from beluno.worker.runtime import get_email_sender, get_worker_runtime

app = procrastinate.App(connector=procrastinate.PsycopgConnector())

WORKER_QUEUES = ["maintenance", EMAIL_QUEUE]


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


@app.periodic(cron="41 3 * * *", periodic_id="sync.compact_changes")
@app.task(
    name="sync.compact_changes",
    queue="maintenance",
    retry=3,
    queueing_lock="sync:compact_changes",
)
async def compact_sync_changes(timestamp: int) -> int:
    """Daily: drop change rows past retention and raise each scope's compaction floor."""

    del timestamp
    return await compact_changes(get_worker_runtime())


@app.periodic(cron="53 3 * * *", periodic_id="sync.purge_operations")
@app.task(
    name="sync.purge_operations",
    queue="maintenance",
    retry=3,
    queueing_lock="sync:purge_operations",
)
async def purge_sync_operations(timestamp: int) -> int:
    """Daily: remove idempotency records whose retention has elapsed."""

    del timestamp
    return await purge_operations(get_worker_runtime())


@app.periodic(cron="*/5 * * * *", periodic_id="jobs.report_queue_health")
@app.task(name="jobs.report_queue_health", queue="maintenance", queueing_lock="jobs:health")
async def report_queue_health(timestamp: int) -> dict[str, float]:
    """Every 5 minutes: publish queue depths and the oldest waiting age as a gauge."""

    del timestamp
    return (await queue_health(get_worker_runtime())).as_measurements()


@app.periodic(cron="29 4 * * *", periodic_id="finance.reconcile_ledgers")
@app.task(
    name="finance.reconcile_ledgers",
    queue="maintenance",
    retry=3,
    queueing_lock="finance:reconcile_ledgers",
)
async def reconcile_finance_ledgers(timestamp: int) -> int:
    """Daily: recompute every plan ledger from postings; any finding is an incident."""

    del timestamp
    return await reconcile_ledgers(get_worker_runtime())


@app.periodic(cron="11 5 * * *", periodic_id="finance.ingest_market_rates")
@app.task(
    name="finance.ingest_market_rates",
    queue="maintenance",
    retry=3,
    queueing_lock="finance:ingest_market_rates",
)
async def ingest_finance_market_rates(timestamp: int) -> int:
    """Daily: store the market rates the provider published (estimates for clients only)."""

    del timestamp
    return await ingest_market_rates(get_worker_runtime(), rate_provider())


@app.periodic(cron="47 3 * * *", periodic_id="activity.purge_events")
@app.task(
    name="activity.purge_events",
    queue="maintenance",
    retry=3,
    queueing_lock="activity:purge_events",
)
async def purge_activity_events(timestamp: int) -> int:
    """Daily: drop feed events older than the change-log retention."""

    del timestamp
    return await purge_activity(get_worker_runtime())
