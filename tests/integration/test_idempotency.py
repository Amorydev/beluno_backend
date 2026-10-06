"""Idempotent command execution: replay, key reuse, concurrency, retries, and conflicts."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import psycopg
import pytest
from pydantic import BaseModel, ConfigDict

from beluno.auth import AuthenticatedActor
from beluno.config import Settings
from beluno.contracts.errors import validation_error
from beluno.db.ids import new_id
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.iam import rate_limits
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation
from beluno.sync.commands import Command, CommandCall, CommandRegistry
from beluno.sync.executor import MAX_ATTEMPTS, CommandRunner
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: object) -> dict:
    response = await api.post(
        "/v1/plans", json={"title": "Dinner", "base_currency": "USD", **body}, headers=owner.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def total_hits(admin: AdminDatabase) -> int:
    return int(admin.scalar("SELECT coalesce(sum(hits), 0) FROM iam.rate_limit_counters"))


def change_count(admin: AdminDatabase, scope_id: str) -> int:
    return int(
        admin.scalar("SELECT count(*) FROM sync_audit.change_log WHERE scope_id = %s", scope_id)
    )


async def test_replay_returns_the_stored_outcome_without_a_second_mutation(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    headers = {**owner.headers, "Idempotency-Key": "create-1"}
    body = {"title": "Dinner", "base_currency": "USD"}

    first = await api.post("/v1/plans", json=body, headers=headers)
    second = await api.post("/v1/plans", json=body, headers=headers)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["ETag"] == second.headers["ETag"] == '"1"'
    assert "Idempotency-Replayed" not in first.headers
    assert second.headers["Idempotency-Replayed"] == "true"
    plan_id = first.json()["id"]
    assert admin.scalar("SELECT count(*) FROM plans.plans") == 1
    assert change_count(admin, plan_id) == 2  # plan.created + owner participant
    assert admin.fetch(
        "SELECT command, idempotency_key, response_status, source FROM sync_audit.operations"
    ) == [("plan.create", "create-1", 201, "http")]
    # plan.created, the owner participant, and the owner's own plan_access signal
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.change_log WHERE operation_id = "
            "(SELECT id FROM sync_audit.operations)"
        )
        == 3
    )

    # A later update with a key: the replay carries the same body and ETag as the original.
    update_headers = {**owner.headers, "If-Match": '"1"', "Idempotency-Key": "rename-1"}
    renamed = await api.patch(
        f"/v1/plans/{plan_id}", json={"title": "Lunch"}, headers=update_headers
    )
    replayed = await api.patch(
        f"/v1/plans/{plan_id}", json={"title": "Lunch"}, headers=update_headers
    )
    assert renamed.status_code == replayed.status_code == 200
    assert renamed.json() == replayed.json()
    assert replayed.headers["ETag"] == '"2"'
    assert replayed.headers["Idempotency-Replayed"] == "true"
    # Without the key, the stale If-Match is rejected as usual.
    stale = await api.patch(
        f"/v1/plans/{plan_id}",
        json={"title": "Lunch"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert stale.status_code == 412


async def test_same_key_with_a_different_request_is_rejected(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    headers = {**owner.headers, "Idempotency-Key": "k1"}
    first = await api.post(
        "/v1/plans", json={"title": "Dinner", "base_currency": "USD"}, headers=headers
    )
    assert first.status_code == 201

    reused = await api.post(
        "/v1/plans", json={"title": "Breakfast", "base_currency": "USD"}, headers=headers
    )
    assert reused.status_code == 409
    assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert admin.fetch("SELECT title FROM plans.plans") == [("Dinner",)]

    # Keys are scoped per command and per actor.
    other_command = await api.post(
        "/v1/groups",
        json={"name": "Crew", "default_currency": "USD", "default_timezone": "UTC"},
        headers=headers,
    )
    assert other_command.status_code == 201
    stranger = await sign_in(api, identity_provider, name="Stranger")
    strangers = await api.post(
        "/v1/plans",
        json={"title": "Breakfast", "base_currency": "USD"},
        headers={**stranger.headers, "Idempotency-Key": "k1"},
    )
    assert strangers.status_code == 201


async def test_concurrent_duplicates_execute_once(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    headers = {**owner.headers, "Idempotency-Key": "burst"}
    body = {"title": "Dinner", "base_currency": "USD"}

    responses = await asyncio.gather(
        *(api.post("/v1/plans", json=body, headers=headers) for _ in range(5))
    )

    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["id"] for response in responses}) == 1
    assert (
        sum(response.headers.get("Idempotency-Replayed") == "true" for response in responses) == 4
    )
    assert admin.scalar("SELECT count(*) FROM plans.plans") == 1
    assert admin.scalar("SELECT count(*) FROM sync_audit.operations") == 1


async def test_failed_commands_store_nothing_and_can_be_retried(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    headers = {**owner.headers, "Idempotency-Key": "state-1", "If-Match": '"1"'}

    invalid = await api.post(
        f"/v1/plans/{plan['id']}/state", json={"state": "completed"}, headers=headers
    )
    assert invalid.status_code == 409
    assert admin.scalar("SELECT count(*) FROM sync_audit.operations") == 0

    # The same key may be used again once the request is valid.
    valid = await api.post(
        f"/v1/plans/{plan['id']}/state", json={"state": "active"}, headers=headers
    )
    assert valid.status_code == 200, valid.text
    assert admin.scalar("SELECT count(*) FROM sync_audit.operations") == 1


async def test_version_conflict_carries_the_current_snapshot(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner, title="Original")
    await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "Renamed"},
        headers={**owner.headers, "If-Match": '"1"'},
    )

    stale = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "Too late"},
        headers={**owner.headers, "If-Match": '"1"'},
    )

    assert stale.status_code == 412
    problem = stale.json()
    assert problem["code"] == "VERSION_CONFLICT"
    assert problem["current"]["title"] == "Renamed"
    assert problem["current"]["version"] == 2
    assert problem["current"]["my_participant"]["role"] == "owner"
    current = await api.get(f"/v1/plans/{plan['id']}", headers=owner.headers)
    assert current.json() == problem["current"]

    travel = await api.put(
        f"/v1/plans/{plan['id']}/travel", json={"notes": "first"}, headers=owner.headers
    )
    assert travel.status_code == 200
    stale_travel = await api.put(
        f"/v1/plans/{plan['id']}/travel",
        json={"notes": "second"},
        headers={**owner.headers, "If-Match": '"9"'},
    )
    assert stale_travel.status_code == 412
    assert stale_travel.json()["current"]["notes"] == "first"


async def test_client_generated_ids_are_honoured_and_duplicates_rejected(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    await api.put(f"/v1/plans/{plan['id']}/travel", json={"notes": "x"}, headers=owner.headers)

    segment_id = str(new_id())
    segment = {
        "id": segment_id,
        "segment_type": "car",
        "timing_mode": "date",
        "start_date": "2026-12-01",
    }
    created = await api.post(
        f"/v1/plans/{plan['id']}/travel/segments", json=segment, headers=owner.headers
    )
    assert created.status_code == 201, created.text
    assert created.json()["id"] == segment_id
    duplicate = await api.post(
        f"/v1/plans/{plan['id']}/travel/segments", json=segment, headers=owner.headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ALREADY_EXISTS"

    participant_id = str(new_id())
    seeded = await api.post(
        f"/v1/plans/{plan['id']}/participants",
        json={"id": participant_id, "placeholder_name": "Grandma"},
        headers=owner.headers,
    )
    assert seeded.status_code == 201, seeded.text
    assert seeded.json()["id"] == participant_id
    clash = await api.post(
        f"/v1/plans/{plan['id']}/participants",
        json={"id": participant_id, "placeholder_name": "Grandpa"},
        headers=owner.headers,
    )
    assert clash.status_code == 409

    series_id = str(new_id())
    series_body = {
        "id": series_id,
        "title": "Weekly",
        "base_currency": "USD",
        "timezone": "UTC",
        "start_date": "2027-01-04",
        "recurrence_rule": "FREQ=WEEKLY",
    }
    series = await api.post("/v1/plan-series", json=series_body, headers=owner.headers)
    assert series.status_code == 201, series.text
    assert series.json()["series"]["id"] == series_id
    again = await api.post("/v1/plan-series", json=series_body, headers=owner.headers)
    assert again.status_code == 409


async def test_replays_do_not_count_against_rate_limits(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    headers = {**owner.headers, "Idempotency-Key": "dup-1"}
    body = {"timing": {"mode": "undecided"}}
    hits_before = total_hits(admin)

    first = await api.post(f"/v1/plans/{plan['id']}/duplicate", json=body, headers=headers)
    assert first.status_code == 201, first.text
    for _ in range(3):
        replay = await api.post(f"/v1/plans/{plan['id']}/duplicate", json=body, headers=headers)
        assert replay.status_code == 201
        assert replay.headers["Idempotency-Replayed"] == "true"

    assert total_hits(admin) - hits_before == 1
    assert admin.scalar("SELECT count(*) FROM plans.plans") == 2


async def test_disabled_commands_are_refused(
    live_settings: Settings, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    from beluno.api.main import create_app
    from beluno.db.session import Database

    settings = live_settings.model_copy(update={"sync_disabled_commands": ["plan.duplicate"]})
    database = Database(settings)
    app = create_app(
        settings=settings, database=database, identity_verifier=identity_provider.verifier(settings)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as api:
        owner = await sign_in(api, identity_provider, name="Owner")
        plan = await make_plan(api, owner)
        refused = await api.post(
            f"/v1/plans/{plan['id']}/duplicate",
            json={"timing": {"mode": "undecided"}},
            headers=owner.headers,
        )
    await database.close()

    assert refused.status_code == 503
    assert refused.json()["code"] == "FEATURE_DISABLED"
    assert admin.scalar("SELECT count(*) FROM plans.plans") == 1


# --- executor-level behaviour exercised with purpose-built commands ---------------


class ProbePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class ProbeResponse(BaseModel):
    label: str
    version: int


def probe_command(handler) -> Command[ProbePayload, ProbeResponse]:  # type: ignore[no-untyped-def]
    return Command(
        name="probe.record",
        payload_model=ProbePayload,
        response_model=ProbeResponse,
        handler=handler,
        status=201,
        etag=lambda body: int(body.version),
    )


async def _unused_presenter(ctx: CommandContext, entity: object) -> None:
    return None


async def signed_in_actor(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, runtime: Runtime
) -> AuthenticatedActor:
    signed_in = await sign_in(api, identity_provider, name="Prober")
    return runtime.tokens.verify(signed_in.access_token)


async def test_deadlocks_are_retried_in_a_fresh_transaction(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    runtime: Runtime,
    live_settings: Settings,
    admin: AdminDatabase,
) -> None:
    actor = await signed_in_actor(api, identity_provider, runtime)
    scope_id = new_id()
    attempts: list[int] = []
    helper_waiting = asyncio.Event()
    helper_done = asyncio.Event()
    dsn = live_settings.api_database_dsn
    assert dsn is not None

    async def helper() -> None:
        # Takes lock 2, then lock 1 after the handler holds lock 1 and waits for 2.
        async with await psycopg.AsyncConnection.connect(
            dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        ) as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(2)")
            helper_waiting.set()
            await asyncio.sleep(0.3)
            await connection.execute("SELECT pg_advisory_xact_lock(1)")
            await connection.commit()
        helper_done.set()

    async def handler(
        ctx: CommandContext, call: CommandCall, payload: ProbePayload
    ) -> ProbeResponse:
        attempts.append(len(attempts) + 1)
        await record_mutation(
            ctx,
            action="probe.recorded",
            entity_type="probe",
            entity_id=new_id(),
            entity_version=len(attempts),
            scope=ChangeScope.PLAN,
            scope_id=scope_id,
        )
        if len(attempts) == 1:
            from sqlalchemy import text

            await ctx.session.execute(text("SELECT pg_advisory_xact_lock(1)"))
            helper_task = asyncio.create_task(helper())
            await helper_waiting.wait()
            # Deadlock: this transaction waits for 2 while the helper will wait for 1.
            try:
                await ctx.session.execute(text("SELECT pg_advisory_xact_lock(2)"))
            finally:
                ctx.pending_helper = helper_task  # type: ignore[attr-defined]
        return ProbeResponse(label=payload.label, version=len(attempts))

    runner = CommandRunner(
        runtime, CommandRegistry([probe_command(handler)], present_conflict=_unused_presenter)
    )
    result = await runner.run(actor, probe_command(handler), CommandCall(), ProbePayload(label="x"))
    await asyncio.wait_for(helper_done.wait(), timeout=10)

    assert attempts == [1, 2]
    assert result.body.version == 2
    # Only the successful attempt left records behind.
    assert admin.fetch(
        "SELECT entity_version FROM sync_audit.change_log WHERE scope_id = %s", scope_id
    ) == [(2,)]


async def test_retries_stop_after_the_bounded_attempts(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, runtime: Runtime
) -> None:
    actor = await signed_in_actor(api, identity_provider, runtime)
    attempts: list[int] = []

    async def handler(
        ctx: CommandContext, call: CommandCall, payload: ProbePayload
    ) -> ProbeResponse:
        attempts.append(1)
        raise psycopg.errors.lookup("40001")("could not serialize access")

    runner = CommandRunner(
        runtime, CommandRegistry([probe_command(handler)], present_conflict=_unused_presenter)
    )
    with pytest.raises(psycopg.errors.SerializationFailure):
        await runner.run(actor, probe_command(handler), CommandCall(), ProbePayload(label="x"))
    assert len(attempts) == MAX_ATTEMPTS


async def test_domain_errors_are_never_retried(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, runtime: Runtime
) -> None:
    actor = await signed_in_actor(api, identity_provider, runtime)
    attempts: list[int] = []

    async def handler(
        ctx: CommandContext, call: CommandCall, payload: ProbePayload
    ) -> ProbeResponse:
        attempts.append(1)
        raise validation_error("nope")

    runner = CommandRunner(
        runtime, CommandRegistry([probe_command(handler)], present_conflict=_unused_presenter)
    )
    with pytest.raises(Exception, match="nope"):
        await runner.run(actor, probe_command(handler), CommandCall(), ProbePayload(label="x"))
    assert len(attempts) == 1


async def test_registry_rejects_unknown_commands_and_other_versions() -> None:
    registry = CommandRegistry(
        [probe_command(lambda ctx, call, payload: None)], present_conflict=_unused_presenter
    )
    assert registry.resolve("probe.record", 1).name == "probe.record"
    with pytest.raises(Exception) as unknown:
        registry.resolve("probe.unknown", 1)
    assert getattr(unknown.value, "code", None) == "VALIDATION_FAILED"
    with pytest.raises(Exception) as outdated:
        registry.resolve("probe.record", 2)
    assert getattr(outdated.value, "code", None) == "CLIENT_UPGRADE_REQUIRED"
    assert getattr(outdated.value, "status", None) == 426
    with pytest.raises(ValueError, match="duplicate"):
        CommandRegistry(
            [probe_command(lambda c, k, p: None), probe_command(lambda c, k, p: None)],
            present_conflict=_unused_presenter,
        )


async def test_rate_limited_commands_still_count_failed_attempts(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    hits_before = total_hits(admin)
    missing = await api.post(
        f"/v1/plans/{uuid4()}/duplicate",
        json={"timing": {"mode": "undecided"}},
        headers=owner.headers,
    )
    assert missing.status_code == 404
    assert total_hits(admin) - hits_before == 1
    assert rate_limits.DUPLICATION_PER_USER.limit > 1
