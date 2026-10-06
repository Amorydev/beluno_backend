from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

from beluno.api.main import create_app
from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.bootstrap import bootstrap_job_schema, run_migrations
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import (
    RUNTIME_PASSWORDS,
    IntegrationEnvironment,
    RecordingEmailSender,
    build_settings,
    role_dsn,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher


class HealthyDatabase:
    async def check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class UnhealthyDatabase:
    async def check(self) -> bool:
        return False

    async def close(self) -> None:
        return None


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(
        app=create_app(settings=Settings(), database=HealthyDatabase()),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
async def degraded_client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(
        app=create_app(settings=Settings(), database=UnhealthyDatabase()),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client


# --- Live PostgreSQL --------------------------------------------------------
# Uses BELUNO_TEST_ADMIN_DATABASE_URL (a disposable local cluster) when set,
# otherwise Testcontainers when Docker is available, otherwise skips. Each test
# session gets a fresh database with reviewed migrations, the job schema, and
# least-privilege runtime roles; tables are truncated before each test.

TRUNCATE_SQL = """
TRUNCATE
    sync_audit.audit_events, sync_audit.change_log, sync_audit.scope_heads,
    sync_audit.operations,
    plans.travel_segments, plans.travel_plan_details, plans.plan_invites, groups.group_invites,
    plans.plan_participants, plans.plans, plans.plan_series,
    groups.group_memberships, groups.groups,
    iam.rate_limit_counters, iam.email_challenges, iam.refresh_tokens, iam.sessions,
    iam.user_identities, iam.users,
    jobs.procrastinate_events, jobs.procrastinate_jobs
CASCADE
"""


def _docker_is_available() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], check=False, capture_output=True).returncode == 0
    )


def _provision_roles(admin_dsn: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        for role_name, password in RUNTIME_PASSWORDS.items():
            exists = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)
            ).fetchone()
            statement = "ALTER" if exists else "CREATE"
            connection.execute(
                sql.SQL(
                    statement + " ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(password))
            )
        for role_name in ("support_readonly", "restore_validator"):
            exists = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)
            ).fetchone()
            if not exists:
                connection.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name))
                )


def _create_database(admin_dsn: str, name: str) -> str:
    identifier = sql.Identifier(name)
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(identifier))
    database_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(database_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(identifier)
        )
        connection.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO migrator, api_runtime, worker_runtime, "
                "scheduler_runtime"
            ).format(identifier)
        )
        connection.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO migrator").format(identifier))
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO migrator")
    return database_dsn


def _drop_database(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
        )
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def admin_cluster_dsn() -> Iterator[str]:
    configured = os.environ.get("BELUNO_TEST_ADMIN_DATABASE_URL")
    if configured:
        yield configured.replace("postgresql+psycopg://", "postgresql://", 1)
        return
    if not _docker_is_available():
        pytest.skip("Set BELUNO_TEST_ADMIN_DATABASE_URL or start Docker for live PostgreSQL")
    from testcontainers.community.postgres import PostgresContainer

    with ExitStack() as stack:
        container = stack.enter_context(
            PostgresContainer("postgres:16-alpine", username="postgres", password="postgres")
        )
        yield container.get_connection_url().replace("+psycopg2", "")


@pytest.fixture(scope="session")
def integration_environment(admin_cluster_dsn: str) -> Iterator[IntegrationEnvironment]:
    _provision_roles(admin_cluster_dsn)
    name = f"beluno_it_{uuid4().hex[:12]}"
    database_admin_dsn = _create_database(admin_cluster_dsn, name)
    settings = build_settings(
        api_dsn=role_dsn(database_admin_dsn, "api_runtime"),
        worker_dsn=role_dsn(database_admin_dsn, "worker_runtime"),
        scheduler_dsn=role_dsn(database_admin_dsn, "scheduler_runtime"),
        migration_dsn=role_dsn(database_admin_dsn, "migrator"),
    )
    try:
        run_migrations(settings)
        asyncio.run(bootstrap_job_schema(settings))
        yield IntegrationEnvironment(settings=settings, admin_dsn=database_admin_dsn)
    finally:
        _drop_database(admin_cluster_dsn, name)


@pytest.fixture
def environment(integration_environment: IntegrationEnvironment) -> IntegrationEnvironment:
    with psycopg.connect(integration_environment.admin_dsn, autocommit=True) as connection:
        connection.execute(TRUNCATE_SQL)
    return integration_environment


@pytest.fixture
def live_settings(environment: IntegrationEnvironment) -> Settings:
    return environment.settings


@pytest.fixture
def identity_provider() -> IdentityProviderStub:
    return IdentityProviderStub()


@pytest.fixture
def email_sender() -> RecordingEmailSender:
    return RecordingEmailSender()


@pytest.fixture
async def api(
    live_settings: Settings,
    identity_provider: IdentityProviderStub,
) -> AsyncIterator[httpx.AsyncClient]:
    database = Database(live_settings)
    app = create_app(
        settings=live_settings,
        database=database,
        identity_verifier=identity_provider.verifier(live_settings),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await database.close()


@pytest.fixture
def admin(environment: IntegrationEnvironment) -> AdminDatabase:
    return AdminDatabase(environment.admin_dsn)


@pytest.fixture
async def runtime(live_settings: Settings) -> AsyncIterator[Runtime]:
    """An API-role runtime for exercising services below the HTTP layer."""

    database = Database(live_settings)
    yield Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    await database.close()


@pytest.fixture
def scratch_database(admin_cluster_dsn: str) -> Iterator[IntegrationEnvironment]:
    """An empty database with runtime-role grants and no migrations applied."""

    _provision_roles(admin_cluster_dsn)
    name = f"beluno_scratch_{uuid4().hex[:12]}"
    database_admin_dsn = _create_database(admin_cluster_dsn, name)
    settings = build_settings(
        api_dsn=role_dsn(database_admin_dsn, "api_runtime"),
        worker_dsn=role_dsn(database_admin_dsn, "worker_runtime"),
        scheduler_dsn=role_dsn(database_admin_dsn, "scheduler_runtime"),
        migration_dsn=role_dsn(database_admin_dsn, "migrator"),
    )
    try:
        yield IntegrationEnvironment(settings=settings, admin_dsn=database_admin_dsn)
    finally:
        _drop_database(admin_cluster_dsn, name)
