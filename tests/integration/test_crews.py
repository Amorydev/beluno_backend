"""Crews: private, saved lists of the people a user plans with."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import if_match, join_with_invite, pull_all
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def trip_with(api: httpx.AsyncClient, owner: SignedIn, *people: SignedIn) -> str:
    created = await api.post(
        "/v1/plans",
        json={"type": "trip", "title": "Da Lat", "base_currency": "VND"},
        headers=owner.headers,
    )
    assert created.status_code == 201, created.text
    plan_id: str = created.json()["id"]
    for person in people:
        await join_with_invite(api, owner, plan_id, person)
    return plan_id


async def create_crew(api: httpx.AsyncClient, owner: SignedIn, **body: Any) -> httpx.Response:
    return await api.post("/v1/crews", json={"name": "Hotpot gang", **body}, headers=owner.headers)


def members(crew: dict[str, Any]) -> dict[str, tuple[str | None, bool]]:
    return {m["user_id"]: (m["display_name"], m["addable"]) for m in crew["members"]}


async def test_a_crew_lists_people_you_plan_with_and_starts_new_plans(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    linh = await sign_in(api, identity_provider, name="Linh")
    minh = await sign_in(api, identity_provider, name="Minh")
    stranger = await sign_in(api, identity_provider, name="Stranger")
    await trip_with(api, linh, minh)

    refused = await create_crew(api, linh, member_user_ids=[minh.user_id, stranger.user_id])
    assert refused.status_code == 422
    repeated = await create_crew(api, linh, member_user_ids=[minh.user_id, minh.user_id])
    assert repeated.status_code == 422
    both = await create_crew(api, linh, member_user_ids=[minh.user_id], from_plan_id=str(uuid4()))
    assert both.status_code == 422

    created = await create_crew(api, linh, member_user_ids=[linh.user_id, minh.user_id])
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"1"'
    crew = created.json()
    assert members(crew) == {linh.user_id: ("Linh", False), minh.user_id: ("Minh", True)}
    assert crew["source_plan_id"] is None

    # Starting a plan from the crew adds the addable people directly.
    seeds = [{"user_id": m["user_id"]} for m in crew["members"] if m["addable"]]
    started = await api.post(
        "/v1/plans",
        json={"type": "hangout", "title": "Hotpot", "base_currency": "VND", "participants": seeds},
        headers=linh.headers,
    )
    assert started.status_code == 201, started.text
    roster = await api.get(f"/v1/plans/{started.json()['id']}/participants", headers=linh.headers)
    assert sorted(p["display_name"] for p in roster.json()) == ["Linh", "Minh"]


async def test_saving_a_plan_keeps_everyone_active_in_it(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    linh = await sign_in(api, identity_provider, name="Linh")
    minh = await sign_in(api, identity_provider, name="Minh")
    leaver = await sign_in(api, identity_provider, name="Leaver")
    plan_id = await trip_with(api, linh, minh, leaver)
    invite = await api.post(f"/v1/plans/{plan_id}/invites", json={}, headers=linh.headers)
    guest = await api.post(
        "/v1/invites/redeem", json={"token": invite.json()["token"], "display_name": "Guest"}
    )
    assert guest.status_code == 200, guest.text
    await api.post(f"/v1/plans/{plan_id}/leave", headers=leaver.headers)

    saved = await create_crew(api, linh, name="Da Lat crew", from_plan_id=plan_id)
    assert saved.status_code == 201, saved.text
    crew = saved.json()
    assert crew["source_plan_id"] == plan_id
    # Crews list registered people only; guests join new plans through an invite link.
    assert members(crew) == {linh.user_id: ("Linh", False), minh.user_id: ("Minh", True)}

    outsider = await sign_in(api, identity_provider, name="Outsider")
    hidden = await create_crew(api, outsider, from_plan_id=plan_id)
    assert hidden.status_code == 404


async def test_crews_belong_to_registered_accounts_and_list_registered_people(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, live_settings: Settings
) -> None:
    linh = await sign_in(api, identity_provider, name="Linh")
    minh = await sign_in(api, identity_provider, name="Minh")
    plan_id = await trip_with(api, linh, minh)
    invite = await api.post(f"/v1/plans/{plan_id}/invites", json={}, headers=linh.headers)
    redeemed = await api.post(
        "/v1/invites/redeem", json={"token": invite.json()["token"], "display_name": "Guest"}
    )
    assert redeemed.status_code == 200, redeemed.text
    guest = signed_in_from(redeemed.json()["session"])

    refused = await create_crew(api, guest, member_user_ids=[minh.user_id])
    assert refused.status_code == 403
    assert (await api.get("/v1/crews", headers=guest.headers)).json() == []
    with_guest = await create_crew(api, linh, member_user_ids=[minh.user_id, guest.user_id])
    assert with_guest.status_code == 422
    crew = (await create_crew(api, linh, member_user_ids=[minh.user_id])).json()
    grown = await api.patch(
        f"/v1/crews/{crew['id']}",
        json={"member_user_ids": [minh.user_id, guest.user_id]},
        headers=if_match(1, linh),
    )
    assert grown.status_code == 422

    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    insert = (
        "INSERT INTO people.crews (id, owner_user_id, name, member_user_ids, version, "
        "created_at, updated_at) VALUES (%s, %s, 'Crew', %s::uuid[], 1, now(), now())"
    )
    with psycopg.connect(dsn) as connection:
        for actor, params in (
            (guest, (str(new_id()), guest.user_id, [guest.user_id])),
            (linh, (str(new_id()), linh.user_id, [guest.user_id])),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor.user_id,))
                connection.execute(insert, params)


async def test_crews_stay_private_to_their_owner(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    linh = await sign_in(api, identity_provider, name="Linh")
    minh = await sign_in(api, identity_provider, name="Minh")
    await trip_with(api, linh, minh)
    crew = (await create_crew(api, linh, member_user_ids=[minh.user_id])).json()
    path = f"/v1/crews/{crew['id']}"

    assert (await api.get("/v1/crews", headers=minh.headers)).json() == []
    assert (await api.get(path, headers=minh.headers)).status_code == 404
    renamed = await api.patch(path, json={"name": "Mine now"}, headers=if_match(1, minh))
    assert renamed.status_code == 404
    assert (await api.delete(path, headers=minh.headers)).status_code == 404
    items, _, _ = await pull_all(api, minh, f"user:{minh.user_id}")
    assert [item for item in items if item["entity_type"] == "crew"] == []

    mine = await api.get("/v1/crews", headers=linh.headers)
    assert [row["id"] for row in mine.json()] == [crew["id"]]
    fetched = await api.get(path, headers=linh.headers)
    assert fetched.headers["etag"] == '"1"' and fetched.json()["name"] == "Hotpot gang"


async def test_owners_rename_change_people_and_delete_through_rest_and_sync(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    linh = await sign_in(api, identity_provider, name="Linh")
    minh = await sign_in(api, identity_provider, name="Minh")
    an = await sign_in(api, identity_provider, name="An")
    stranger = await sign_in(api, identity_provider, name="Stranger")
    plan_id = await trip_with(api, linh, minh, an)
    crew = (await create_crew(api, linh, member_user_ids=[minh.user_id])).json()
    path = f"/v1/crews/{crew['id']}"
    scope = f"user:{linh.user_id}"
    snapshot, cursor, _ = await pull_all(api, linh, scope)
    assert [
        (i["entity_id"], i["data"]["name"]) for i in snapshot if i["entity_type"] == "crew"
    ] == [(crew["id"], "Hotpot gang")]

    stale = await api.patch(path, json={"name": "Old"}, headers=if_match(3, linh))
    assert stale.status_code == 412
    missing = await api.patch(path, json={"name": "No version"}, headers=linh.headers)
    assert missing.status_code == 428
    empty = await api.patch(path, json={}, headers=if_match(1, linh))
    assert empty.status_code == 422
    outsider = await api.patch(
        path, json={"member_user_ids": [minh.user_id, stranger.user_id]}, headers=if_match(1, linh)
    )
    assert outsider.status_code == 422

    grown = await api.patch(
        path,
        json={"name": "Japan crew", "member_user_ids": [minh.user_id, an.user_id]},
        headers=if_match(1, linh),
    )
    assert grown.status_code == 200, grown.text
    assert (grown.json()["name"], grown.json()["version"]) == ("Japan crew", 2)
    assert grown.headers["etag"] == '"2"'

    # People already listed may stay after they stop sharing a plan; they are no longer addable.
    await api.post(f"/v1/plans/{plan_id}/leave", headers=an.headers)
    kept = await api.patch(path, json={"name": "Japan crew 2026"}, headers=if_match(2, linh))
    assert kept.status_code == 200, kept.text
    assert members(kept.json())[an.user_id] == ("An", False)

    changes, cursor, _ = await pull_all(api, linh, scope, cursor)
    assert [(i["entity_type"], i["operation"], i["version"]) for i in changes] == [
        ("crew", "upsert", 3)
    ]

    pushed = await api.post(
        "/v1/sync/push",
        json={
            "operations": [
                {
                    "operation_id": str(new_id()),
                    "command": "crew.delete",
                    "schema_version": 1,
                    "target": {"crew_id": crew["id"]},
                    "payload": {},
                    "client_created_at": "2026-10-06T08:00:00Z",
                }
            ]
        },
        headers=linh.headers,
    )
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["results"][0]["outcome"] == "applied"
    assert (await api.get(path, headers=linh.headers)).status_code == 404
    assert (await api.get("/v1/crews", headers=linh.headers)).json() == []
    # The tombstone keeps neither the name nor the people.
    assert admin.fetch(
        "SELECT name, member_user_ids FROM people.crews WHERE id = %s", crew["id"]
    ) == [("", [])]
    deleted, _, _ = await pull_all(api, linh, scope, cursor)
    assert [(i["entity_type"], i["operation"], i["version"]) for i in deleted] == [
        ("crew", "delete", 4)
    ]
    assert (await api.delete(path, headers=linh.headers)).status_code == 404


async def test_the_database_refuses_strangers_and_other_owners(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, live_settings: Settings
) -> None:
    linh = await sign_in(api, identity_provider, name="Linh")
    minh = await sign_in(api, identity_provider, name="Minh")
    stranger = await sign_in(api, identity_provider, name="Stranger")
    await trip_with(api, linh, minh)
    crew = (await create_crew(api, linh, member_user_ids=[minh.user_id])).json()
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    insert = (
        "INSERT INTO people.crews (id, owner_user_id, name, member_user_ids, version, "
        "created_at, updated_at) VALUES (%s, %s, 'Crew', %s::uuid[], 1, now(), now())"
    )
    with psycopg.connect(dsn) as connection:
        for statement, params in (
            (insert, (str(new_id()), linh.user_id, [stranger.user_id])),
            (
                "UPDATE people.crews SET member_user_ids = %s::uuid[] WHERE id = %s",
                ([minh.user_id, stranger.user_id], crew["id"]),
            ),
            (
                "UPDATE people.crews SET owner_user_id = %s WHERE id = %s",
                (minh.user_id, crew["id"]),
            ),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (linh.user_id,))
                connection.execute(statement, params)
        with connection.transaction():
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (minh.user_id,))
            assert connection.execute("SELECT count(*) FROM people.crews").fetchone() == (0,)
            updated = connection.execute(
                "UPDATE people.crews SET name = 'Taken' WHERE id = %s", (crew["id"],)
            )
            assert updated.rowcount == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("DELETE FROM people.crews")
