"""Procrastinate worker entrypoint."""

from __future__ import annotations

import asyncio

from beluno.config import get_settings
from beluno.worker.connection import open_worker_pool
from beluno.worker.tasks import WORKER_QUEUES, app


async def run() -> None:
    settings = get_settings()
    async with open_worker_pool(settings) as pool, app.open_async(pool=pool):
        await app.run_worker_async(queues=WORKER_QUEUES)


if __name__ == "__main__":
    asyncio.run(run())
