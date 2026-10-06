"""Change-log sequencing, buffering, and the database gates around it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic.config import Config

from alembic import command
from beluno.config import Settings
from beluno.db.bootstrap import PROJECT_ROOT
from beluno.db.ids import new_id
from beluno.modules.context import CommandContext, Runtime, open_context
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation
from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import IntegrationEnvironment
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

APPEND = "SELECT sync_audit.append_changes(%s::jsonb)"


def raw_dsn(dsn: str | None) -> str:
    assert dsn is not None
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def change(
    scope_id: UUID,
    *,
    entity_id: UUID | None = None,
    scope_type: str = "plan",
    changed_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "changed_at": (changed_at or datetime.now(UTC)).isoformat(),
        "scope_type": scope_type,
        "scope_id": str(scope_id),
        "entity_type": "probe",
        "entity_id": str(entity_id or uuid4()),
        "entity_version": 1,
        "operation": "upsert",
    }


def scope_rows(admin: AdminDatabase, scope_id: UUID | str) -> list[tuple[int, str, int]]:
    return [
        (seq, str(entity_id), version)
        for seq, entity_id, version in admin.fetch(
            "SELECT scope_seq, entity_id, entity_version FROM sync_audit.change_log "
            "WHERE scope_id = %s ORDER BY scope_seq",
            scope_id,
        )
    ]


async def wait_for_lock_wait(admin: AdminDatabase) -> None:
    """Barrier: return once another backend is blocked on a lock in this database."""

    for _ in range(500):
        if admin.scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no backend started waiting on a lock")


@pytest.fixture
async def api_connections(
    live_settings: Settings,
) -> AsyncIterator[tuple[psycopg.AsyncConnection, psycopg.AsyncConnection]]:
    dsn = raw_dsn(live_settings.api_database_dsn)
    async with (
        await psycopg.AsyncConnection.connect(dsn) as first,
        await psycopg.AsyncConnection.connect(dsn) as second,
    ):
        yield first, second


async def test_api_mutations_get_contiguous_sequences_per_scope(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    created = [
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": title, "base_currency": "USD"},
            headers=owner.headers,
        )
        for title in ("Trip", "Dinner")
    ]
    first, second = (response.json() for response in created)
    for version, title in ((1, "Trip 2"), (2, "Trip 3")):
        renamed = await api.patch(
            f"/v1/plans/{first['id']}",
            json={"title": title},
            headers={**owner.headers, "If-Match": f'"{version}"'},
        )
        assert renamed.status_code == 200, renamed.text
    retitled = await api.patch(
        f"/v1/plans/{second['id']}",
        json={"title": "Late dinner"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert retitled.status_code == 200

    first_rows = scope_rows(admin, first["id"])
    assert [seq for seq, _, _ in first_rows] == [1, 2, 3, 4]
    assert [version for _, entity_id, version in first_rows if entity_id == first["id"]] == [
        1,
        2,
        3,
    ]
    second_rows = scope_rows(admin, second["id"])
    assert [seq for seq, _, _ in second_rows] == [1, 2, 3]
    assert [version for _, entity_id, version in second_rows if entity_id == second["id"]] == [
        1,
        2,
    ]
    heads = dict(
        admin.fetch(
            "SELECT scope_id::text, last_seq FROM sync_audit.scope_heads "
            "WHERE scope_id = ANY(%s::uuid[])",
            [first["id"], second["id"]],
        )
    )
    assert heads == {first["id"]: 4, second["id"]: 3}
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.scope_heads WHERE floor_seq <> 0 OR generation <> 1"
        )
        == 0
    )


async def test_failed_command_records_nothing(runtime: Runtime, admin: AdminDatabase) -> None:
    scope_id = new_id()
    with pytest.raises(LookupError):
        async with open_context(runtime) as ctx:
            await record_mutation(
                ctx,
                action="probe.recorded",
                entity_type="probe",
                entity_id=new_id(),
                entity_version=1,
                scope=ChangeScope.PLAN,
                scope_id=scope_id,
            )
            raise LookupError("command failed after recording")

    assert admin.scalar("SELECT count(*) FROM sync_audit.audit_events") == 0
    assert admin.scalar("SELECT count(*) FROM sync_audit.change_log") == 0
    assert admin.scalar("SELECT count(*) FROM sync_audit.scope_heads") == 0


async def test_savepoint_rollback_discards_only_its_records(
    runtime: Runtime, admin: AdminDatabase
) -> None:
    scope_id = new_id()
    kept_first, dropped, kept_last = new_id(), new_id(), new_id()

    async def record(ctx: CommandContext, entity_id: UUID) -> None:
        await record_mutation(
            ctx,
            action="probe.recorded",
            entity_type="probe",
            entity_id=entity_id,
            entity_version=1,
            scope=ChangeScope.PLAN,
            scope_id=scope_id,
        )

    async with open_context(runtime) as ctx:
        await record(ctx, kept_first)
        with suppress(ValueError):
            async with ctx.savepoint():
                await record(ctx, dropped)
                raise ValueError("savepoint rolls back")
        await record(ctx, kept_last)

    assert scope_rows(admin, scope_id) == [(1, str(kept_first), 1), (2, str(kept_last), 1)]
    audited = {
        str(entity_id)
        for (entity_id,) in admin.fetch("SELECT entity_id FROM sync_audit.audit_events")
    }
    assert audited == {str(kept_first), str(kept_last)}


async def test_recording_inside_an_untracked_savepoint_is_refused(runtime: Runtime) -> None:
    async with open_context(runtime) as ctx, ctx.session.begin_nested():
        with pytest.raises(RuntimeError, match="savepoint"):
            await record_mutation(
                ctx,
                action="probe.recorded",
                entity_type="probe",
                entity_id=new_id(),
                entity_version=1,
                scope=ChangeScope.PLAN,
                scope_id=new_id(),
            )


async def test_concurrent_writers_never_expose_a_sequence_gap(
    api_connections: tuple[psycopg.AsyncConnection, psycopg.AsyncConnection],
    admin: AdminDatabase,
) -> None:
    first, second = api_connections
    scope_id, first_entity, second_entity = uuid4(), uuid4(), uuid4()
    await first.execute(APPEND, [json.dumps([change(scope_id, entity_id=first_entity)])])

    async def second_writer() -> None:
        await second.execute(APPEND, [json.dumps([change(scope_id, entity_id=second_entity)])])
        await second.commit()

    blocked = asyncio.create_task(second_writer())
    await wait_for_lock_wait(admin)
    assert not blocked.done()
    # Nothing is visible while the first writer holds the scope head.
    assert scope_rows(admin, scope_id) == []
    await first.commit()
    await asyncio.wait_for(blocked, timeout=10)

    assert scope_rows(admin, scope_id) == [(1, str(first_entity), 1), (2, str(second_entity), 1)]


async def test_scope_heads_are_locked_in_sorted_order(
    api_connections: tuple[psycopg.AsyncConnection, psycopg.AsyncConnection],
    admin: AdminDatabase,
) -> None:
    first, second = api_connections
    low, high = sorted((uuid4(), uuid4()), key=str)
    await first.execute(APPEND, [json.dumps([change(low)])])

    async def second_writer() -> None:
        # Listed high-first; the gate still locks ``low`` first and holds nothing else
        # while it waits, so the first writer can still take ``high``.
        await second.execute(APPEND, [json.dumps([change(high), change(low)])])
        await second.commit()

    blocked = asyncio.create_task(second_writer())
    await wait_for_lock_wait(admin)
    await asyncio.wait_for(first.execute(APPEND, [json.dumps([change(high)])]), timeout=10)
    await first.commit()
    await asyncio.wait_for(blocked, timeout=10)

    assert [seq for seq, _, _ in scope_rows(admin, low)] == [1, 2]
    assert [seq for seq, _, _ in scope_rows(admin, high)] == [1, 2]


async def test_runtime_roles_reach_change_rows_only_through_gates(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    live_settings: Settings,
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    outsider = await sign_in(api, identity_provider, name="Outsider")
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Private", "base_currency": "USD"},
            headers=owner.headers,
        )
    ).json()

    with psycopg.connect(raw_dsn(live_settings.api_database_dsn)) as connection:
        for statement in (
            "SELECT count(*) FROM sync_audit.change_log",
            "INSERT INTO sync_audit.change_log (changed_at, scope_type, scope_id, scope_seq, "
            "entity_type, entity_id, entity_version, operation) VALUES (now(), 'plan', "
            f"'{plan['id']}', 99, 'plan', '{plan['id']}', 9, 'upsert')",
            "UPDATE sync_audit.scope_heads SET last_seq = 0",
            "SELECT sync_audit.compact_changes(now() - interval '1 year', 10, 90)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute(statement)

        def read_as(user_id: str) -> tuple[int, int]:
            with connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (user_id,))
                changes = connection.execute(
                    "SELECT count(*) FROM sync_audit.read_changes('plan', %s, 0, 100, 100)",
                    (plan["id"],),
                ).fetchone()
                heads = connection.execute(
                    "SELECT count(*) FROM sync_audit.scope_heads WHERE scope_id = %s",
                    (plan["id"],),
                ).fetchone()
            assert changes is not None and heads is not None
            return changes[0], heads[0]

        assert read_as(owner.user_id) == (2, 1)
        assert read_as(outsider.user_id) == (0, 0)

    with psycopg.connect(raw_dsn(live_settings.worker_database_dsn)) as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), worker.transaction():
            worker.execute(
                "SELECT * FROM sync_audit.read_changes('plan', %s, 0, 1, 1)", (plan["id"],)
            )
        with worker.transaction():
            worker.execute(APPEND, [json.dumps([change(UUID(plan["id"]))])])


async def test_compaction_keeps_the_offline_window_and_raises_the_floor(
    live_settings: Settings, admin: AdminDatabase
) -> None:
    scope_id = uuid4()
    long_ago = datetime.now(UTC) - timedelta(days=400)
    recent = datetime.now(UTC) - timedelta(days=10)
    with (
        psycopg.connect(raw_dsn(live_settings.api_database_dsn)) as api_connection,
        api_connection.transaction(),
    ):
        api_connection.execute(
            APPEND,
            [
                json.dumps(
                    [
                        change(scope_id, changed_at=long_ago),
                        change(scope_id, changed_at=long_ago),
                        change(scope_id, changed_at=recent),
                    ]
                )
            ],
        )

    with psycopg.connect(raw_dsn(live_settings.worker_database_dsn)) as worker:
        for cutoff_days, window_days in ((30, 90), (100, 120), (60, 1), (60, None)):
            # Inside the configured window, and never below the 90-day product floor.
            with pytest.raises(psycopg.errors.InvalidParameterValue), worker.transaction():
                worker.execute(
                    "SELECT sync_audit.compact_changes(now() - make_interval(days => %s), 100, %s)",
                    (cutoff_days, window_days),
                )
        with worker.transaction():
            removed = worker.execute(
                "SELECT sync_audit.compact_changes(now() - interval '180 days', 100, 90)"
            ).fetchone()
    assert removed == (2,)
    assert [seq for seq, _, _ in scope_rows(admin, scope_id)] == [3]
    assert admin.fetch(
        "SELECT last_seq, floor_seq FROM sync_audit.scope_heads WHERE scope_id = %s", scope_id
    ) == [(3, 2)]


def test_migration_backfills_sequences_and_heads(scratch_database: IntegrationEnvironment) -> None:
    settings = scratch_database.settings
    assert settings.migration_database_dsn is not None
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.migration_database_dsn)
    command.upgrade(config, "000003_tenant_write_guards")

    admin = AdminDatabase(scratch_database.admin_dsn)
    plan_a, plan_b = uuid4(), uuid4()
    for scope_id, version in ((plan_a, 1), (plan_b, 1), (plan_a, 2), (plan_a, 3), (plan_b, 2)):
        admin.execute(
            "INSERT INTO sync_audit.change_log (changed_at, scope_type, scope_id, entity_type, "
            "entity_id, entity_version, operation) VALUES (now(), 'plan', %s, 'plan', %s, %s, "
            "'upsert')",
            scope_id,
            scope_id,
            version,
        )

    command.upgrade(config, "head")

    assert [(seq, version) for seq, _, version in scope_rows(admin, plan_a)] == [
        (1, 1),
        (2, 2),
        (3, 3),
    ]
    assert [(seq, version) for seq, _, version in scope_rows(admin, plan_b)] == [(1, 1), (2, 2)]
    heads = admin.fetch(
        "SELECT scope_id::text, last_seq, floor_seq, generation FROM sync_audit.scope_heads"
    )
    assert sorted(heads) == sorted([(str(plan_a), 3, 0, 1), (str(plan_b), 2, 0, 1)])
