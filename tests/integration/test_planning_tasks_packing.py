"""Tasks move along by their assignee; packing lists are shared or private to their owner."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, finance_plan, if_match, pull_all
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan", "Eve"))


async def add(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    created: dict[str, Any] = await ok(
        await api.post(trip.path(path), json=body, headers=user.headers), 201
    )
    return created


async def push(
    api: httpx.AsyncClient, user: SignedIn, command: str, target: dict[str, str], payload: Any
) -> dict[str, Any]:
    body = await ok(
        await api.post(
            "/v1/sync/push",
            json={
                "device_id": "phone-1",
                "operations": [
                    {
                        "operation_id": str(new_id()),
                        "command": command,
                        "schema_version": 1,
                        "target": target,
                        "depends_on": [],
                        "payload": payload,
                    }
                ],
            },
            headers=user.headers,
        )
    )
    result: dict[str, Any] = body["results"][0]
    return result


async def test_the_assignee_moves_a_task_and_its_creator_edits_it(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea, dan, eve = trip.owner, *(trip.members[n] for n in ("Bea", "Dan", "Eve"))
    _, cursor, _ = await pull_all(api, eve, f"plan:{trip.plan_id}")
    booking = await add(api, owner, trip, "/bookings", {"kind": "lodging", "title": "Ryokan"})
    task = await add(
        api,
        bea,
        trip,
        "/tasks",
        {
            "title": "Print the voucher",
            "assignee_participant_id": trip.people["Dan"],
            "booking_id": booking["id"],
            "due_date": "2027-03-19",
            "due_time": "18:00:00",
            "due_timezone": "Asia/Tokyo",
            "remind_at": "2027-03-19T00:00:00Z",
        },
    )
    assert (task["status"], task["completed_at"], task["version"]) == ("open", None, 1)
    path = trip.path(f"/tasks/{task['id']}")

    # Dan, the assignee, moves it along but does not rewrite it; Eve does neither.
    edit = {"title": "Print both vouchers", "assignee_participant_id": trip.people["Dan"]}
    assert (await api.put(path, json=edit, headers=if_match(1, dan))).status_code == 403
    status = f"{path}/status"
    assert (await api.post(status, json={"status": "done"}, headers=eve.headers)).status_code == 403
    done = await ok(await api.post(status, json={"status": "done"}, headers=dan.headers))
    assert (done["status"], done["completed_by_user_id"], done["version"]) == (
        "done",
        dan.user_id,
        2,
    )
    again = await ok(await api.post(status, json={"status": "done"}, headers=owner.headers))
    assert (again["version"], again["completed_at"]) == (2, done["completed_at"])

    # Bea, who added it, rewrites it (the status stays unless named); stale is refused.
    assert (await api.put(path, json=edit, headers=if_match(1, bea))).status_code == 412
    kept = await ok(await api.put(path, json=edit, headers=if_match(2, bea)))
    assert (kept["status"], kept["completed_at"], kept["booking_id"]) == (
        "done",
        done["completed_at"],
        None,
    )
    reopened = await ok(
        await api.put(path, json={**edit, "status": "open"}, headers=if_match(3, bea))
    )
    assert (reopened["status"], reopened["completed_at"], reopened["completed_by_user_id"]) == (
        "open",
        None,
        None,
    )

    # An offline "done" lands without a version; the feed says so once.
    pushed = await push(
        api,
        dan,
        "task.set_status",
        {"plan_id": trip.plan_id, "task_id": task["id"]},
        {"status": "done"},
    )
    assert pushed["outcome"] == "applied", pushed
    items, cursor, _ = await pull_all(api, eve, f"plan:{trip.plan_id}", cursor)
    synced = [i for i in items if i["entity_type"] == "task"]
    assert synced[-1]["data"]["status"] == "done" and synced[-1]["data"]["version"] == 5
    feed = [i["data"] for i in items if i["entity_type"] == "activity_event"]
    assert [(e["type"], e["actor_user_id"]) for e in feed if e["type"] == "task.completed"] == [
        ("task.completed", dan.user_id),
        ("task.completed", dan.user_id),
    ]

    assert (await api.delete(path, headers=eve.headers)).status_code == 403
    assert (await api.delete(path, headers=bea.headers)).status_code == 204
    assert (await api.get(path, headers=owner.headers)).status_code == 404
    items, _, _ = await pull_all(api, eve, f"plan:{trip.plan_id}", cursor)
    assert [(i["entity_id"], i["operation"]) for i in items if i["entity_type"] == "task"] == [
        (task["id"], "delete")
    ]


async def test_tasks_refuse_bad_links_and_due_times(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, trip: FinancePlan
) -> None:
    owner = trip.owner
    stop = await add(api, owner, trip, "/itinerary", {"title": "Temple"})
    booking = await add(api, owner, trip, "/bookings", {"kind": "other", "title": "Tickets"})
    refused = [
        {"title": "x", "item_id": stop["id"], "booking_id": booking["id"]},
        {"title": "x", "due_date": "2027-03-19", "due_time": "18:00:00"},
        {"title": "x", "due_timezone": "Asia/Tokyo"},
        {"title": "x", "remind_at": "2027-03-19T00:00:00"},
        {"title": "x", "assignee_participant_id": str(new_id())},
        {"title": "x", "item_id": str(new_id())},
        {"title": "x", "booking_id": str(new_id())},
        # A due time that does not exist in the zone (the clocks spring forward).
        {
            "title": "x",
            "due_date": "2027-03-14",
            "due_time": "02:30:00",
            "due_timezone": "America/New_York",
        },
    ]
    for body in refused:
        response = await api.post(trip.path("/tasks"), json=body, headers=owner.headers)
        assert response.status_code == 422, (body, response.text)

    # A linked item deleted later does not block editing the task.
    task = await add(api, owner, trip, "/tasks", {"title": "Book a guide", "item_id": stop["id"]})
    deleted = await api.delete(trip.path(f"/itinerary/{stop['id']}"), headers=owner.headers)
    assert deleted.status_code == 204, deleted.text
    renamed = await ok(
        await api.put(
            trip.path(f"/tasks/{task['id']}"),
            json={"title": "Book a guide early", "item_id": stop["id"]},
            headers=if_match(1, owner),
        )
    )
    assert renamed["item_id"] == stop["id"]

    hangout = await ok(
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Karaoke", "base_currency": "VND"},
            headers=owner.headers,
        ),
        201,
    )
    for path, body in (("/tasks", {"title": "x"}), ("/packing", {"name": "x"})):
        response = await api.post(
            f"/v1/plans/{hangout['id']}{path}", json=body, headers=owner.headers
        )
        assert response.status_code == 409
        assert response.json()["code"] == "NOT_AVAILABLE_FOR_HANGOUT"


async def test_anyone_packs_the_shared_list_and_private_items_stay_private(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea, dan, eve = trip.owner, *(trip.members[n] for n in ("Bea", "Dan", "Eve"))
    await ok(
        await api.patch(
            trip.path(f"/participants/{trip.people['Eve']}"),
            json={"role": "viewer"},
            headers=if_match(1, owner),
        )
    )
    _, plan_cursor, _ = await pull_all(api, bea, f"plan:{trip.plan_id}")
    _, dan_cursor, _ = await pull_all(api, dan, f"user:{dan.user_id}")

    stove = await add(
        api,
        bea,
        trip,
        "/packing",
        {"name": "Camp stove", "category": "gear", "bringer_participant_id": trip.people["Dan"]},
    )
    assert (stove["visibility"], stove["owner_user_id"]) == ("shared", None)
    path = trip.path(f"/packing/{stove['id']}")
    # Eve, a viewer, ticks it but adds nothing to the shared list; Dan cannot rewrite it.
    packed = await ok(await api.post(f"{path}/packed", json={"packed": True}, headers=eve.headers))
    assert (packed["packed"], packed["version"]) == (True, 2)
    assert (
        await api.post(trip.path("/packing"), json={"name": "Tent"}, headers=eve.headers)
    ).status_code == 403
    assert (
        await api.put(path, json={"name": "Two stoves"}, headers=if_match(2, dan))
    ).status_code == 403
    await ok(
        await api.put(path, json={"name": "Two stoves", "quantity": 2}, headers=if_match(2, owner))
    )

    # Dan's private item: only Dan sees, ticks, or syncs it, and only in his user scope.
    socks = await add(api, dan, trip, "/packing", {"name": "Wool socks", "visibility": "private"})
    assert socks["owner_user_id"] == dan.user_id
    mine = f"/packing/{socks['id']}"
    for user in (bea, owner):
        listed = await ok(await api.get(trip.path("/packing"), headers=user.headers))
        assert socks["id"] not in {row["id"] for row in listed}
        assert (
            await api.post(trip.path(f"{mine}/packed"), json={"packed": True}, headers=user.headers)
        ).status_code == 404
    assert (
        await api.post(
            trip.path("/packing"),
            json={
                "name": "Map",
                "visibility": "private",
                "bringer_participant_id": trip.people["Bea"],
            },
            headers=dan.headers,
        )
    ).status_code == 422
    viewer_own = await add(api, eve, trip, "/packing", {"name": "Book", "visibility": "private"})
    assert viewer_own["owner_user_id"] == eve.user_id

    plan_items, plan_cursor, _ = await pull_all(api, bea, f"plan:{trip.plan_id}", plan_cursor)
    assert {i["entity_id"] for i in plan_items if i["entity_type"] == "packing_item"} == {
        stove["id"]
    }
    dan_items, dan_cursor, _ = await pull_all(api, dan, f"user:{dan.user_id}", dan_cursor)
    assert [
        (i["entity_id"], i["operation"]) for i in dan_items if i["entity_type"] == "packing_item"
    ] == [(socks["id"], "upsert")]

    # Sharing moves it: out of Dan's scope, into the plan's, for everyone.
    shared = await ok(await api.post(trip.path(f"{mine}/share"), headers=dan.headers))
    assert (shared["visibility"], shared["owner_user_id"]) == ("shared", None)
    assert socks["id"] in {
        row["id"] for row in await ok(await api.get(trip.path("/packing"), headers=bea.headers))
    }
    dan_items, _, _ = await pull_all(api, dan, f"user:{dan.user_id}", dan_cursor)
    assert [
        (i["entity_id"], i["operation"]) for i in dan_items if i["entity_type"] == "packing_item"
    ] == [(socks["id"], "delete")]
    plan_items, _, _ = await pull_all(api, bea, f"plan:{trip.plan_id}", plan_cursor)
    assert [
        (i["entity_id"], i["operation"]) for i in plan_items if i["entity_type"] == "packing_item"
    ] == [(socks["id"], "upsert")]
    # The creator still edits it; others now only tick it.
    await ok(
        await api.put(trip.path(mine), json={"name": "Wool socks x3"}, headers=if_match(2, dan))
    )
    assert (await api.delete(trip.path(mine), headers=bea.headers)).status_code == 403
    assert (await api.delete(trip.path(mine), headers=dan.headers)).status_code == 204


async def test_a_template_fills_each_list_once(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    bea, dan = trip.members["Bea"], trip.members["Dan"]
    template = {
        "template_id": "beach.v1",
        "items": [
            {"name": "Sunscreen", "category": "toiletries"},
            {"name": "Towel", "category": "gear", "quantity": 2},
        ],
    }

    async def apply(user: SignedIn, **extra: Any) -> list[dict[str, Any]]:
        body = await ok(
            await api.post(
                trip.path("/packing/templates"), json={**template, **extra}, headers=user.headers
            )
        )
        items: list[dict[str, Any]] = body["items"]
        return items

    shared = await apply(bea)
    assert [(i["name"], i["template_id"], i["visibility"]) for i in shared] == [
        ("Sunscreen", "beach.v1", "shared"),
        ("Towel", "beach.v1", "shared"),
    ]
    assert {i["id"] for i in await apply(dan)} == {i["id"] for i in shared}
    for user in (bea, dan):
        private = await apply(user, visibility="private")
        assert {i["owner_user_id"] for i in private} == {user.user_id}
        assert {i["id"] for i in await apply(user, visibility="private")} == {
            i["id"] for i in private
        }
    assert (
        admin.scalar(
            "SELECT count(*) FROM coordination.packing_items WHERE plan_id = %s", trip.plan_id
        )
        == 6
    )
    assert (
        admin.scalar(
            "SELECT count(*) FROM coordination.template_applications WHERE plan_id = %s",
            trip.plan_id,
        )
        == 3
    )


async def test_the_database_keeps_tasks_and_packing_honest(
    api: httpx.AsyncClient, trip: FinancePlan, live_settings: Settings
) -> None:
    owner, bea, eve = trip.owner, trip.members["Bea"], trip.members["Eve"]
    task = await add(
        api,
        owner,
        trip,
        "/tasks",
        {"title": "Rent a car", "assignee_participant_id": trip.people["Bea"]},
    )
    stove = await add(api, owner, trip, "/packing", {"name": "Stove"})
    socks = await add(api, owner, trip, "/packing", {"name": "Socks", "visibility": "private"})
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    attempts = [
        # Eve, not the assignee, ticking off the task.
        (
            eve.user_id,
            "UPDATE coordination.tasks SET status = 'done', completed_at = now(), "
            "completed_by_user_id = %s, version = version + 1 WHERE id = %s",
            (eve.user_id, task["id"]),
        ),
        # Bea, the assignee, renaming it.
        (
            bea.user_id,
            "UPDATE coordination.tasks SET title = 'Mine', version = version + 1 WHERE id = %s",
            (task["id"],),
        ),
        # An organiser recording someone else as having completed it.
        (
            owner.user_id,
            "UPDATE coordination.tasks SET status = 'done', completed_at = now(), "
            "completed_by_user_id = %s, version = version + 1 WHERE id = %s",
            (bea.user_id, task["id"]),
        ),
        # A task in someone else's name.
        (
            bea.user_id,
            "INSERT INTO coordination.tasks (id, plan_id, title, status, created_by_user_id, "
            "version, created_at, updated_at) VALUES (gen_random_uuid(), %s, 'x', 'open', %s, "
            "1, now(), now())",
            (trip.plan_id, owner.user_id),
        ),
        # Renaming someone else's shared item, or hiding it as one's own.
        (
            bea.user_id,
            "UPDATE coordination.packing_items SET name = 'Mine', version = version + 1 "
            "WHERE id = %s",
            (stove["id"],),
        ),
        (
            owner.user_id,
            "UPDATE coordination.packing_items SET visibility = 'private', owner_user_id = %s, "
            "version = version + 1 WHERE id = %s",
            (owner.user_id, stove["id"]),
        ),
        # A private item for someone else.
        (
            bea.user_id,
            "INSERT INTO coordination.packing_items (id, plan_id, visibility, owner_user_id, "
            "name, category, quantity, packed, created_by_user_id, version, created_at, "
            "updated_at) VALUES (gen_random_uuid(), %s, 'private', %s, 'x', 'other', 1, false, "
            "%s, 1, now(), now())",
            (trip.plan_id, owner.user_id, bea.user_id),
        ),
    ]
    with psycopg.connect(dsn) as connection:
        for actor, statement, params in attempts:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor,))
                connection.execute(statement, params)
        # Someone else's private item is invisible, so nothing to update.
        with connection.transaction():
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (bea.user_id,))
            cursor = connection.execute(
                "UPDATE coordination.packing_items SET packed = true, version = version + 1 "
                "WHERE id = %s",
                (socks["id"],),
            )
            assert cursor.rowcount == 0


async def test_tasks_and_private_lists_follow_people_who_merge_claim_leave_or_go(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    admin: AdminDatabase,
) -> None:
    owner, bea, dan, eve = trip.owner, *(trip.members[n] for n in ("Bea", "Dan", "Eve"))
    _, cursor, _ = await pull_all(api, eve, f"plan:{trip.plan_id}")

    # A task made done at once says so; one for placeholder Cam is Bea's once she is Cam.
    await add(api, owner, trip, "/tasks", {"title": "Book flights", "status": "done"})
    task = await add(
        api,
        owner,
        trip,
        "/tasks",
        {"title": "Buy SIMs", "assignee_participant_id": trip.people["Cam"]},
    )
    claim = await add(api, owner, trip, f"/participants/{trip.people['Cam']}/claim-invites", {})
    await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": claim["token"], "merge_existing": True},
            headers=bea.headers,
        )
    )
    moved = await ok(
        await api.post(
            trip.path(f"/tasks/{task['id']}/status"), json={"status": "done"}, headers=bea.headers
        )
    )
    assert moved["completed_by_user_id"] == bea.user_id
    items, _, _ = await pull_all(api, eve, f"plan:{trip.plan_id}", cursor)
    completed = [
        i["data"]["actor_user_id"]
        for i in items
        if i["entity_type"] == "activity_event" and i["data"]["type"] == "task.completed"
    ]
    assert completed == [owner.user_id, bea.user_id]

    # Dan leaves: his private list stays readable to him, so his devices and a fresh
    # snapshot agree; he can no longer change it.
    socks = await add(api, dan, trip, "/packing", {"name": "Socks", "visibility": "private"})
    _, dan_cursor, _ = await pull_all(api, dan, f"user:{dan.user_id}")
    left = await api.post(trip.path("/leave"), headers=dan.headers)
    assert left.status_code == 204, left.text
    snapshot, _, _ = await pull_all(api, dan, f"user:{dan.user_id}")
    assert socks["id"] in {i["entity_id"] for i in snapshot if i["entity_type"] == "packing_item"}
    later, _, _ = await pull_all(api, dan, f"user:{dan.user_id}", dan_cursor)
    assert [i for i in later if i["entity_type"] == "packing_item"] == []
    assert (
        await api.post(
            trip.path(f"/packing/{socks['id']}/packed"), json={"packed": True}, headers=dan.headers
        )
    ).status_code in (403, 404)

    # A guest's private list follows them into the account they sign in to.
    invite = await add(api, owner, trip, "/invites", {})
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": invite["token"], "display_name": "Gia", "device": {"platform": "web"}},
    )
    assert joined.status_code == 200, joined.text
    guest = signed_in_from(joined.json()["session"])
    template = {"template_id": "city", "visibility": "private", "items": [{"name": "Charger"}]}
    applied = await ok(
        await api.post(trip.path("/packing/templates"), json=template, headers=guest.headers)
    )
    charger = applied["items"][0]["id"]
    fay = await sign_in(api, identity_provider, subject="fay-sub", name="Fay")
    _, fay_cursor, _ = await pull_all(api, fay, f"user:{fay.user_id}")
    claimed = await api.post(
        "/v1/auth/google",
        json={"id_token": identity_provider.id_token(subject="fay-sub")},
        headers=guest.headers,
    )
    assert claimed.status_code == 200, claimed.text
    fay = signed_in_from(claimed.json())
    listed = await ok(await api.get(trip.path("/packing"), headers=fay.headers))
    assert [(r["id"], r["owner_user_id"]) for r in listed if r["visibility"] == "private"] == [
        (charger, fay.user_id)
    ]
    again = await ok(
        await api.post(trip.path("/packing/templates"), json=template, headers=fay.headers)
    )
    assert [i["id"] for i in again["items"]] == [charger]
    fay_items, _, _ = await pull_all(api, fay, f"user:{fay.user_id}", fay_cursor)
    assert [
        (i["entity_id"], i["operation"]) for i in fay_items if i["entity_type"] == "packing_item"
    ] == [(charger, "upsert")]

    # Deleting an account deletes its private lists.
    await add(api, eve, trip, "/packing", {"name": "Insulin", "visibility": "private"})
    gone = await api.delete("/v1/me", headers=eve.headers)
    assert gone.status_code == 204, gone.text
    assert (
        admin.scalar(
            "SELECT count(*) FROM coordination.packing_items WHERE owner_user_id = %s",
            eve.user_id,
        )
        == 0
    )
