"""Live PostgreSQL proof for the platform boundary and runtime privileges."""

from __future__ import annotations

import psycopg
import pytest

from beluno.testkit.environment import IntegrationEnvironment
from beluno.worker.connection import open_worker_pool
from beluno.worker.tasks import app as job_app
from beluno.worker.tasks import heartbeat

pytestmark = pytest.mark.integration


def test_fresh_database_bootstrap_enforces_runtime_boundaries(
    environment: IntegrationEnvironment,
) -> None:
    settings = environment.settings
    with psycopg.connect(environment.admin_dsn) as connection:
        schemas = {
            row[0]
            for row in connection.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ('iam', 'plans', 'finance', 'jobs')"
            )
        }
        assert schemas == {"iam", "plans", "finance", "jobs"}
        assert connection.execute("SELECT to_regclass('jobs.procrastinate_jobs')").fetchone()[0]

    assert settings.api_database_dsn is not None
    api_dsn = settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(api_dsn) as api_connection:
        assert api_connection.execute(
            "SELECT has_schema_privilege(current_user, 'plans', 'USAGE')"
        ).fetchone()[0]
        assert not api_connection.execute(
            "SELECT has_schema_privilege(current_user, 'plans', 'CREATE')"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api_connection.execute("CREATE TABLE plans.forbidden_write (id bigint)")


async def test_scheduler_role_can_enqueue_a_job(environment: IntegrationEnvironment) -> None:
    async with (
        open_worker_pool(environment.settings, role="scheduler") as pool,
        job_app.open_async(pool=pool),
    ):
        job_id = await heartbeat.defer_async(release="integration", payload_version=1)
    assert job_id > 0
