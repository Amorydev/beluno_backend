"""Explicit, single-run platform bootstrap for migrations and the job queue."""

from __future__ import annotations

from pathlib import Path

import procrastinate
from alembic.config import Config
from psycopg_pool import AsyncConnectionPool

from alembic import command
from beluno.config import Settings
from beluno.worker.connection import worker_dsn
from beluno.worker.tasks import app as job_app

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROLE_GRANTS_PATH = PROJECT_ROOT / "sql" / "platform-runtime-grants.sql"


def run_migrations(settings: Settings) -> None:
    """Apply reviewed Alembic revisions exactly once under the migrator DSN."""

    if settings.migration_database_dsn is None:
        raise RuntimeError("BELUNO_MIGRATION_DATABASE_URL is required for migrations")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.migration_database_dsn)
    command.upgrade(config, "head")


async def bootstrap_job_schema(settings: Settings) -> None:
    """Create the version-pinned Procrastinate schema and grant queue-only access."""

    if settings.migration_database_dsn is None:
        raise RuntimeError("BELUNO_MIGRATION_DATABASE_URL is required for job bootstrap")
    migration_dsn = settings.migration_database_dsn.replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )
    pool = AsyncConnectionPool(
        conninfo=migration_dsn,
        kwargs={"options": "-csearch_path=jobs,public"},
        min_size=1,
        max_size=1,
        open=False,
    )
    await pool.open(wait=True)
    try:
        async with pool.connection() as connection:
            result = await connection.execute("SELECT to_regclass('jobs.procrastinate_jobs')")
            row = await result.fetchone()
            jobs_table_exists = row is not None and row[0] is not None
        # A throwaway connector keeps the process-wide app free of this closed pool.
        bootstrap_app = job_app.with_connector(procrastinate.PsycopgConnector())
        async with bootstrap_app.open_async(pool=pool):
            if not jobs_table_exists:
                await bootstrap_app.schema_manager.apply_schema_async()
        async with pool.connection() as connection:
            await connection.execute(ROLE_GRANTS_PATH.read_text(encoding="utf-8"))
            await connection.commit()
    finally:
        await pool.close()


async def job_schema_is_ready(settings: Settings) -> bool:
    """Return whether the named queue table is available to a runtime role."""

    pool = AsyncConnectionPool(
        conninfo=worker_dsn(settings),
        kwargs={"options": "-csearch_path=jobs,public"},
        min_size=1,
        max_size=1,
        open=False,
    )
    await pool.open(wait=True)
    try:
        async with pool.connection() as connection:
            result = await connection.execute("SELECT to_regclass('jobs.procrastinate_jobs')")
            row = await result.fetchone()
            return row is not None and row[0] is not None
    finally:
        await pool.close()
