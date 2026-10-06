"""Retention jobs, duplicate-safe enqueue, and dead-letter tooling on real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import IntegrationEnvironment
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher
from beluno.worker import deadletter, tasks
from beluno.worker.enqueue import defer_in_transaction

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    database = Database.for_worker(live_settings)
    runtime = Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    monkeypatch.setattr(tasks, "get_worker_runtime", lambda: runtime)
    yield runtime
    await database.close()


async def test_compaction_job_removes_old_changes_and_raises_floors(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = (
        await api.post(
            "/v1/plans", json={"title": "Old", "base_currency": "USD"}, headers=owner.headers
        )
    ).json()
    fresh = (
        await api.post(
            "/v1/plans", json={"title": "New", "base_currency": "USD"}, headers=owner.headers
        )
    ).json()
    admin.execute(
        "UPDATE sync_audit.change_log SET changed_at = now() - interval '200 days' "
        "WHERE scope_id = %s",
        plan["id"],
    )

    removed = await tasks.compact_sync_changes.func(0)

    assert removed == 2
    assert (
        admin.scalar("SELECT count(*) FROM sync_audit.change_log WHERE scope_id = %s", plan["id"])
        == 0
    )
    assert admin.fetch(
        "SELECT last_seq, floor_seq FROM sync_audit.scope_heads WHERE scope_id = %s", plan["id"]
    ) == [(2, 2)]
    assert (
        admin.scalar(
            "SELECT floor_seq FROM sync_audit.scope_heads WHERE scope_id = %s", fresh["id"]
        )
        == 0
    )
    assert await tasks.compact_sync_changes.func(0) == 0
    # The scope still bootstraps after compaction removed its history.
    pulled = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": f"plan:{plan['id']}", "cursor": None}]},
        headers=owner.headers,
    )
    assert pulled.json()["scopes"][0]["status"] == "ok"


async def test_purge_job_removes_only_expired_operation_records(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    for key in ("old", "new"):
        created = await api.post(
            "/v1/plans",
            json={"title": key, "base_currency": "USD"},
            headers={**owner.headers, "Idempotency-Key": key},
        )
        assert created.status_code == 201
    admin.execute(
        "UPDATE sync_audit.operations SET created_at = now() - interval '200 days', "
        "expires_at = now() - interval '20 days' WHERE idempotency_key = 'old'"
    )

    assert await tasks.purge_sync_operations.func(0) == 1
    assert admin.fetch("SELECT idempotency_key FROM sync_audit.operations") == [("new",)]
    assert await tasks.purge_sync_operations.func(0) == 0


async def test_enqueue_with_a_queueing_lock_is_duplicate_safe(
    runtime: Runtime, admin: AdminDatabase
) -> None:
    async with runtime.database.transaction() as session:
        first = await defer_in_transaction(
            session,
            task_name="maintenance.probe",
            queue="maintenance",
            args={"n": 1},
            queueing_lock="probe:1",
        )
        second = await defer_in_transaction(
            session,
            task_name="maintenance.probe",
            queue="maintenance",
            args={"n": 1},
            queueing_lock="probe:1",
        )
        unlocked = [
            await defer_in_transaction(
                session, task_name="maintenance.probe", queue="maintenance", args={"n": 2}
            )
            for _ in range(2)
        ]
        # The transaction is still usable after the swallowed duplicate.
        await session.execute(__import__("sqlalchemy").text("SELECT 1"))

    assert (first, second, unlocked) == (True, False, [True, True])
    assert (
        admin.scalar(
            "SELECT count(*) FROM jobs.procrastinate_jobs WHERE task_name = 'maintenance.probe'"
        )
        == 3
    )


async def test_dead_letter_listing_retry_and_health_are_audited(
    api: httpx.AsyncClient,
    worker: Runtime,
    admin: AdminDatabase,
    environment: IntegrationEnvironment,
) -> None:
    started = await api.post("/v1/auth/email/challenges", json={"email": "dead@example.com"})
    assert started.status_code == 202
    job_id = admin.scalar("SELECT id FROM jobs.procrastinate_jobs WHERE queue_name = 'email'")
    # Procrastinate's status trigger resolves its types through search_path.
    with psycopg.connect(environment.admin_dsn, autocommit=True) as connection:
        connection.execute("SET search_path = jobs, public")
        connection.execute(
            "UPDATE jobs.procrastinate_jobs SET status = 'failed', attempts = 5 WHERE id = %s",
            (job_id,),
        )
    before = datetime.now(UTC)

    failed = await deadletter.list_jobs(worker, status="failed")
    assert [(job.id, job.task_name, job.attempts) for job in failed] == [
        (job_id, "iam.deliver_email_challenge", 5)
    ]
    assert set(failed[0].args) == {"challenge_id", "payload_version"}
    health = await deadletter.queue_health(worker)
    assert (health.todo, health.doing, health.failed) == (0, 0, 1)

    retried = await deadletter.retry_job(worker, job_id, operator="jane")

    assert retried.status == "todo" and retried.attempts == 6
    assert retried.scheduled_at is not None and retried.scheduled_at >= before
    assert await deadletter.list_jobs(worker, status="failed") == []
    audit = admin.fetch(
        "SELECT action, entity_id, metadata->>'operator', metadata->>'job_id' "
        "FROM sync_audit.audit_events WHERE action = 'job.retried'"
    )
    assert audit == [("job.retried", UUID(int=job_id), "jane", str(job_id))]
    with pytest.raises(LookupError):
        await deadletter.retry_job(worker, job_id, operator="jane")
    with pytest.raises(LookupError):
        await deadletter.retry_job(worker, 999_999, operator="jane")
    after = await tasks.report_queue_health.func(0)
    assert after["todo"] == 1.0 and after["failed"] == 0.0
    assert after["oldest_waiting_seconds"] >= 0.0
