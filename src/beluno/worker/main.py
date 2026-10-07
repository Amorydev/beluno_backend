"""Procrastinate worker entrypoint."""

from __future__ import annotations

import asyncio

from beluno.config import get_settings
from beluno.worker.connection import open_worker_pool
from beluno.worker.runtime import get_worker_runtime
from beluno.worker.tasks import WORKER_QUEUES, app


async def run() -> None:
    settings = get_settings()
    if settings.storage_endpoint_url:
        await get_worker_runtime().storage.ensure_bucket()
    async with open_worker_pool(settings) as pool, app.open_async(pool=pool):
        # A few jobs at once, so a slow scan never holds up a sign-in email.
        await app.run_worker_async(queues=WORKER_QUEUES, concurrency=4)


if __name__ == "__main__":
    asyncio.run(run())
