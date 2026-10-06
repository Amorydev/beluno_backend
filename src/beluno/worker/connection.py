"""Short-lived PostgreSQL pool lifecycle for worker and scheduler processes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from beluno.config import Settings


def worker_dsn(settings: Settings, *, role: str = "worker") -> str:
    """Translate SQLAlchemy's Psycopg URL form to a direct Psycopg DSN."""

    dsn = settings.worker_database_dsn if role == "worker" else settings.scheduler_database_dsn
    if dsn is None:
        variable = (
            "BELUNO_WORKER_DATABASE_URL" if role == "worker" else "BELUNO_SCHEDULER_DATABASE_URL"
        )
        raise RuntimeError(f"{variable} is required for background jobs")
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


@asynccontextmanager
async def open_worker_pool(
    settings: Settings,
    *,
    role: str = "worker",
) -> AsyncIterator[AsyncConnectionPool]:
    """Open and always close a pool owned by the calling process."""

    pool = AsyncConnectionPool(
        conninfo=worker_dsn(settings, role=role),
        kwargs={"options": "-csearch_path=jobs,public"},
        min_size=1,
        max_size=3,
        open=False,
    )
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()
