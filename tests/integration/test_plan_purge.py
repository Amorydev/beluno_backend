"""Plans scheduled for deletion are purged for good once the restore window ends."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import SignedIn
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    exercise_money_and_members,
    finance_plan,
    if_match,
    ledger_balances,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

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


async def schedule_deletion(
    api: httpx.AsyncClient, owner: SignedIn, plan_id: str, admin: AdminDatabase, days_ago: int
) -> None:
    version = (await api.get(f"/v1/plans/{plan_id}", headers=owner.headers)).json()["version"]
    scheduled = await api.delete(f"/v1/plans/{plan_id}", headers=if_match(version, owner))
    assert scheduled.status_code == 200, scheduled.text
    admin.execute(
        "UPDATE plans.plans SET deletion_scheduled_at = now() - make_interval(days => %s) "
        "WHERE id = %s",
        days_ago,
        plan_id,
    )


def rows_left(admin: AdminDatabase, plan_id: str) -> dict[str, int]:
    """Rows still pointing at the plan, per table that has a ``plan_id`` column."""

    tables = admin.fetch(
        "SELECT table_schema || '.' || table_name FROM information_schema.columns "
        "WHERE column_name = 'plan_id' AND table_schema IN ('plans', 'finance', 'activity') "
        "ORDER BY 1"
    )
    counts = {
        table: admin.scalar(f"SELECT count(*) FROM {table} WHERE plan_id = %s", plan_id)
        for (table,) in tables
    }
    counts["plans.plans"] = admin.scalar("SELECT count(*) FROM plans.plans WHERE id = %s", plan_id)
    counts["sync_audit.change_log"] = admin.scalar(
        "SELECT count(*) FROM sync_audit.change_log WHERE scope_type = 'plan' AND scope_id = %s",
        plan_id,
    )
    counts["sync_audit.scope_heads"] = admin.scalar(
        "SELECT count(*) FROM sync_audit.scope_heads WHERE scope_type = 'plan' AND scope_id = %s",
        plan_id,
    )
    return counts


async def test_a_plan_past_its_restore_window_goes_with_everything_it_holds(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    worker: Runtime,
) -> None:
    trip = await exercise_money_and_members(api, identity_provider, admin)
    owner = trip.owner
    committed = await api.post(
        trip.path("/commitments"),
        json={"category": "lodging", "description": "Cabin", "currency": "EUR", "amount_minor": 90},
        headers=owner.headers,
    )
    assert committed.status_code == 201, committed.text
    seq = (await api.get(trip.path("/ledger"), headers=owner.headers)).json()["ledger_seq"]
    confirmed = await api.post(
        trip.path("/ledger/confirm"), json={"ledger_seq": seq}, headers=owner.headers
    )
    assert confirmed.status_code == 200, confirmed.text
    claim = await api.post(
        trip.path(f"/participants/{trip.people['Cam']}/claim-invites"),
        json={},
        headers=owner.headers,
    )
    assert claim.status_code == 201, claim.text
    copy = await api.post(
        trip.path("/duplicate"), json={"title": "Same trip again"}, headers=owner.headers
    )
    assert copy.status_code == 201, copy.text
    neighbour: FinancePlan = await finance_plan(api, identity_provider, admin, members=("Eve",))
    await add_expense(
        api,
        neighbour.owner,
        neighbour,
        equal_expense(1_200, neighbour.people["Ann"], list(neighbour.people.values())),
    )
    untouched = await ledger_balances(api, neighbour.owner, neighbour)
    assert any(rows_left(admin, trip.plan_id).values())
    _, cursor, _ = await pull_all(api, owner, f"user:{owner.user_id}")

    await schedule_deletion(api, owner, trip.plan_id, admin, days_ago=31)
    audited = admin.scalar(
        "SELECT count(*) FROM sync_audit.audit_events WHERE plan_id = %s", trip.plan_id
    )
    assert await tasks.purge_deleted_plan_records.func(0) == 1

    assert {table: count for table, count in rows_left(admin, trip.plan_id).items() if count} == {}
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events WHERE plan_id = %s", trip.plan_id
        )
        == audited
        > 0
    )
    assert admin.fetch(
        "SELECT duplicated_from_plan_id FROM plans.plans WHERE id = %s", copy.json()["id"]
    ) == [(None,)]
    assert (await api.get(trip.path(), headers=owner.headers)).status_code == 404
    # The owner's devices drop the plan; the neighbouring plan is untouched.
    items, _, _ = await pull_all(api, owner, f"user:{owner.user_id}", cursor)
    assert {
        (item["entity_id"], item["operation"])
        for item in items
        if item["entity_type"] == "plan_access" and item["entity_id"] == trip.plan_id
    } == {(trip.plan_id, "delete")}
    assert await ledger_balances(api, neighbour.owner, neighbour) == untouched
    assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", neighbour.plan_id) == []
    assert await tasks.purge_deleted_plan_records.func(0) == 0


async def test_plans_inside_the_restore_window_stay_restorable(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    worker: Runtime,
    live_settings: Settings,
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    await schedule_deletion(api, trip.owner, trip.plan_id, admin, days_ago=29)

    assert await tasks.purge_deleted_plan_records.func(0) == 0
    version = (await api.get(trip.path(), headers=trip.owner.headers)).json()["version"]
    restored = await api.post(trip.path("/restore"), headers=if_match(version, trip.owner))
    assert restored.status_code == 200, restored.text

    # The gate refuses a cutoff that would reach plans still inside any sane window.
    assert live_settings.worker_database_dsn is not None
    dsn = live_settings.worker_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        connection.execute("SELECT plans.purge_deleted_plan(now() - interval '1 day')")


async def test_finance_history_stays_append_only_for_runtime_roles(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    live_settings: Settings,
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    await add_expense(
        api, trip.owner, trip, equal_expense(500, trip.people["Ann"], [trip.people["Ann"]])
    )
    for dsn in (live_settings.api_database_dsn, live_settings.worker_database_dsn):
        assert dsn is not None
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://")) as connection:
            for table in ("expense_revisions", "ledger_postings"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                    connection.execute("SELECT set_config('beluno.plan_purge', 'on', true)")
                    connection.execute(
                        f"DELETE FROM finance.{table} WHERE plan_id = %s", (trip.plan_id,)
                    )
    assert admin.scalar(
        "SELECT count(*) FROM finance.expense_revisions WHERE plan_id = %s", trip.plan_id
    )
