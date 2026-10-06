"""Scheduler entrypoint. Later phases enqueue only idempotent named jobs."""

from __future__ import annotations

import asyncio

from beluno import __version__
from beluno.config import get_settings
from beluno.worker.connection import open_worker_pool
from beluno.worker.tasks import app, heartbeat


async def run_once() -> str:
    settings = get_settings()
    async with open_worker_pool(settings, role="scheduler") as pool, app.open_async(pool=pool):
        job_id = await heartbeat.defer_async(release=__version__, payload_version=1)
    return str(job_id)


if __name__ == "__main__":
    print(asyncio.run(run_once()))
