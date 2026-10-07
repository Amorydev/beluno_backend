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
        "WHERE column_name = 'plan_id' "
        "AND table_schema IN ('plans', 'finance', 'activity', 'schedule_places', 'decisions', "
        "'bookings', 'coordination', 'media_memories') "
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
    place = await api.post(trip.path("/places"), json={"name": "Temple"}, headers=owner.headers)
    assert place.status_code == 201, place.text
    await api.put(
        trip.path(f"/places/{place.json()['id']}/reaction"),
        json={"wants": True},
        headers=owner.headers,
    )
    stop = await api.post(
        trip.path("/itinerary"),
        json={"title": "Temple", "place_id": place.json()["id"]},
        headers=owner.headers,
    )
    assert stop.status_code == 201, stop.text
    await api.put(
        trip.path(f"/itinerary/{stop.json()['id']}/attendance"),
        json={"status": "going"},
        headers=owner.headers,
    )
    booking = await api.post(
        trip.path("/bookings"),
        json={
            "kind": "lodging",
            "title": "Ryokan",
            "place_id": place.json()["id"],
            "price": {"currency": "EUR", "amount_minor": 1_000},
            "secrets": {"confirmation_code": "RYO-1"},
        },
        headers=owner.headers,
    )
    assert booking.status_code == 201, booking.text
    poll = await api.post(
        trip.path("/polls"),
        json={
            "question": "Go?",
            "options": [{"label": "Temple", "place_id": place.json()["id"]}, {"label": "No"}],
        },
        headers=owner.headers,
    )
    assert poll.status_code == 201, poll.text
    await api.put(
        trip.path(f"/polls/{poll.json()['id']}/vote"),
        json={"option_id": poll.json()["options"][0]["id"]},
        headers=owner.headers,
    )
    await api.post(trip.path(f"/polls/{poll.json()['id']}/close"), headers=owner.headers)
    applied = await api.post(
        trip.path(f"/polls/{poll.json()['id']}/outcome"),
        json={"action": "add_to_plan"},
        headers=owner.headers,
    )
    assert applied.status_code == 200, applied.text
    for path, body in (
        ("/tasks", {"title": "Print vouchers", "booking_id": booking.json()["id"]}),
        ("/packing", {"name": "Earplugs", "visibility": "private"}),
    ):
        added = await api.post(trip.path(path), json=body, headers=owner.headers)
        assert added.status_code == 201, added.text
    private_item = added.json()["id"]
    receipt = await api.post(
        trip.path("/media"),
        json={
            "kind": "receipt",
            "content_type": "image/png",
            "size_bytes": 10,
            "expense_id": (await api.get(trip.path("/expenses"), headers=owner.headers)).json()[
                "items"
            ][0]["id"],
        },
        headers=owner.headers,
    )
    assert receipt.status_code == 201, receipt.text
    template = await api.post(
        trip.path("/packing/templates"),
        json={"template_id": "onsen", "items": [{"name": "Yukata"}]},
        headers=owner.headers,
    )
    assert template.status_code == 200, template.text
    crew = await api.post(
        "/v1/crews", json={"name": "Trip crew", "from_plan_id": trip.plan_id}, headers=owner.headers
    )
    assert crew.status_code == 201, crew.text
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
    # Its stored files are queued for the worker to delete from storage.
    assert sorted(admin.fetch("SELECT object_key FROM media_memories.object_deletions")) == [
        (f"incoming/{receipt.json()['id']}",),
        (f"media/{receipt.json()['id']}",),
    ]
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events WHERE plan_id = %s", trip.plan_id
        )
        == audited + 1
        > 1
    )
    assert admin.fetch(
        "SELECT duplicated_from_plan_id FROM plans.plans WHERE id = %s", copy.json()["id"]
    ) == [(None,)]
    assert admin.fetch(
        "SELECT action FROM sync_audit.audit_events WHERE plan_id = %s AND action = 'plan.purged'",
        trip.plan_id,
    ) == [("plan.purged",)]
    saved = (await api.get(f"/v1/crews/{crew.json()['id']}", headers=owner.headers)).json()
    assert saved["source_plan_id"] is None
    assert (await api.get(trip.path(), headers=owner.headers)).status_code == 404
    # The owner's devices drop the plan; the neighbouring plan is untouched.
    items, _, _ = await pull_all(api, owner, f"user:{owner.user_id}", cursor)
    assert {
        (item["entity_id"], item["operation"])
        for item in items
        if item["entity_type"] == "plan_access" and item["entity_id"] == trip.plan_id
    } == {(trip.plan_id, "delete")}
    # So do the private packing items of the plan, kept in their owner's scope.
    assert {
        (item["entity_id"], item["operation"])
        for item in items
        if item["entity_type"] == "packing_item"
    } == {(private_item, "delete")}
    assert (crew.json()["id"], "upsert") in {
        (item["entity_id"], item["operation"]) for item in items if item["entity_type"] == "crew"
    }
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


async def test_nobody_can_bring_a_purge_forward(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    live_settings: Settings,
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    bea = trip.members["Bea"]
    admin.execute(
        "UPDATE plans.plan_participants SET role = 'admin' WHERE id = %s", trip.people["Bea"]
    )
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    backdate = (
        "UPDATE plans.plans SET deletion_scheduled_at = now() - interval '400 days' WHERE id = %s"
    )
    schedule_now = "UPDATE plans.plans SET deletion_scheduled_at = now() WHERE id = %s"
    with psycopg.connect(dsn) as connection:
        for actor, statement in (
            (bea.user_id, schedule_now),  # an admin is not the owner
            (trip.owner.user_id, backdate),  # the owner cannot backdate either
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor,))
                connection.execute(statement, (trip.plan_id,))
    await schedule_deletion(api, trip.owner, trip.plan_id, admin, days_ago=0)
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (trip.owner.user_id,))
        connection.execute(backdate, (trip.plan_id,))


async def test_only_the_purge_gate_deletes_finance_history(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann = trip.people["Ann"]
    await add_expense(api, trip.owner, trip, equal_expense(500, ann, [ann]))
    # Even a role the write guards do not restrict meets the append-only trigger
    # unless the purge flag is raised.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("DELETE FROM finance.expense_revisions WHERE plan_id = %s", trip.plan_id)
