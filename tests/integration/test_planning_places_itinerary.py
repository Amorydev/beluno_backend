"""Saved places and the itinerary: who may do what, ordering, local times, costs, sync."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

GOOGLE = "https://www.google.com/maps/place/Senso-ji/@35.7148,139.7967,17z"


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def save_place(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, **body: Any
) -> dict[str, Any]:
    return await ok(
        await api.post(
            trip.path("/places"), json={"name": "Senso-ji", **body}, headers=user.headers
        ),
        201,
    )


async def add_item(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, **body: Any
) -> dict[str, Any]:
    return await ok(
        await api.post(
            trip.path("/itinerary"), json={"title": "Stop", **body}, headers=user.headers
        ),
        201,
    )


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


async def test_places_are_saved_read_from_links_and_wanted(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    parsed = await save_place(api, bea, trip, maps_url=GOOGLE, category="sight")
    assert (parsed["resolution_state"], parsed["provider"]) == ("parsed", "google")
    assert (parsed["latitude"], parsed["longitude"]) == ("35.714800", "139.796700")
    short = await save_place(api, bea, trip, name="Ramen", maps_url="https://maps.app.goo.gl/x1")
    assert (short["resolution_state"], short["latitude"]) == ("pending", None)
    typed = await save_place(api, owner, trip, name="Market")
    assert (typed["resolution_state"], typed["status"]) == ("manual", "shortlist")

    path = trip.path(f"/places/{parsed['id']}/reaction")
    for user in (owner, dan):
        await ok(await api.put(path, json={"wants": True}, headers=user.headers))
    again = await ok(await api.put(path, json={"wants": True}, headers=dan.headers))
    assert sorted(again["wanted_by"]) == sorted([trip.people["Ann"], trip.people["Dan"]])
    assert again["version"] == 3  # the repeated answer changed nothing

    # Bea saved it; Dan (a member) may not edit it, an organizer may.
    edit = {"name": "Senso-ji temple", "maps_url": GOOGLE, "category": "sight"}
    place_path = trip.path(f"/places/{parsed['id']}")
    refused = await api.put(place_path, json=edit, headers=if_match(3, dan))
    assert refused.status_code == 403
    stale = await api.put(place_path, json=edit, headers=if_match(1, owner))
    assert stale.status_code == 412 and stale.json()["current"]["version"] == 3
    renamed = await ok(await api.put(place_path, json=edit, headers=if_match(3, owner)))
    assert renamed["name"] == "Senso-ji temple"


async def test_viewers_answer_but_do_not_add_and_hangouts_have_no_trip_plan(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, trip: FinancePlan
) -> None:
    dan = trip.members["Dan"]
    await ok(
        await api.patch(
            trip.path(f"/participants/{trip.people['Dan']}"),
            json={"role": "viewer"},
            headers=if_match(1, trip.owner),
        )
    )
    place = await save_place(api, trip.owner, trip)
    assert (
        await api.post(trip.path("/places"), json={"name": "x"}, headers=dan.headers)
    ).status_code == 403
    await ok(
        await api.put(
            trip.path(f"/places/{place['id']}/reaction"), json={"wants": True}, headers=dan.headers
        )
    )
    hangout = await ok(
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Karaoke", "base_currency": "VND"},
            headers=trip.owner.headers,
        ),
        201,
    )
    refused = await api.post(
        f"/v1/plans/{hangout['id']}/places", json={"name": "Bar"}, headers=trip.owner.headers
    )
    assert refused.status_code == 409 and refused.json()["code"] == "NOT_AVAILABLE_FOR_HANGOUT"


async def test_the_itinerary_orders_days_keeps_local_times_and_answers(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    first = await add_item(api, owner, trip, title="Temple", day="2027-03-21")
    second = await add_item(api, bea, trip, title="Lunch", day="2027-03-21")
    assert first["order_key"] < second["order_key"]
    timed = await add_item(
        api,
        owner,
        trip,
        title="Sumo",
        day="2027-03-20",
        start_time="18:30:00",
        timezone="Asia/Tokyo",
        duration_minutes=120,
        lead_participant_id=trip.people["Bea"],
    )
    assert (timed["start_time"], timed["timezone"]) == ("18:30:00", "Asia/Tokyo")
    anytime = await add_item(api, owner, trip, title="Buy a SIM")
    # A local time that the clocks skip is refused.
    skipped = await api.post(
        trip.path("/itinerary"),
        json={
            "title": "Brunch",
            "day": "2027-03-14",
            "start_time": "02:30:00",
            "timezone": "America/New_York",
        },
        headers=owner.headers,
    )
    assert skipped.status_code == 422
    # Move Lunch before Temple on the same day, with a key computed on the device.
    moved = await ok(
        await api.put(
            trip.path(f"/itinerary/{second['id']}"),
            json={"title": "Lunch", "day": "2027-03-21", "order_key": "0V"},
            headers=if_match(1, bea),
        )
    )
    assert moved["order_key"] == "0V"
    listed = await ok(await api.get(trip.path("/itinerary"), headers=owner.headers))
    assert [row["title"] for row in listed] == ["Sumo", "Lunch", "Temple", "Buy a SIM"]

    going = trip.path(f"/itinerary/{timed['id']}/attendance")
    await ok(await api.put(going, json={"status": "going"}, headers=bea.headers))
    answered = await ok(
        await api.put(going, json={"status": "not_going"}, headers=trip.members["Dan"].headers)
    )
    assert sorted((a["participant_id"], a["status"]) for a in answered["attendance"]) == sorted(
        [(trip.people["Bea"], "going"), (trip.people["Dan"], "not_going")]
    )
    bad_lead = await api.post(
        trip.path("/itinerary"),
        json={"title": "x", "lead_participant_id": anytime["id"]},
        headers=owner.headers,
    )
    assert bad_lead.status_code == 422


async def test_planned_costs_count_once_and_a_paid_cost_outlives_its_item(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, ann = trip.owner, trip.people["Ann"]
    cost = {"currency": "USD", "amount_minor": 4_000}
    museum = await add_item(api, owner, trip, title="Museum", day="2027-03-22", estimated_cost=cost)
    ferry = await add_item(api, owner, trip, title="Ferry", estimated_cost=cost)
    assert museum["commitment_id"] and ferry["commitment_id"]

    async def commitments() -> dict[str, str]:
        rows = await ok(await api.get(trip.path("/commitments"), headers=owner.headers))
        return {row["id"]: row["state"] for row in rows}

    assert set((await commitments()).values()) == {"estimated"}
    # Attach an expense to the museum: the expense now carries that cost.
    paid = equal_expense(4_500, ann, [ann], commitment_id=museum["commitment_id"])
    await add_expense(api, owner, trip, paid)
    # Cancelling the museum keeps the money spent; deleting the ferry drops its plan.
    await ok(
        await api.put(
            trip.path(f"/itinerary/{museum['id']}"),
            json={"title": "Museum", "day": "2027-03-22", "status": "cancelled"},
            headers=if_match(1, owner),
        )
    )
    deleted = await api.delete(trip.path(f"/itinerary/{ferry['id']}"), headers=owner.headers)
    assert deleted.status_code == 204
    assert await commitments() == {
        museum["commitment_id"]: "converted_to_expense",
        ferry["commitment_id"]: "cancelled",
    }


async def test_costs_stop_with_the_finance_switch(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_settings, "finance_writes_enabled", False)
    refused = await api.post(
        trip.path("/itinerary"),
        json={"title": "Show", "estimated_cost": {"currency": "USD", "amount_minor": 100}},
        headers=trip.owner.headers,
    )
    assert refused.status_code == 503
    await add_item(api, trip.owner, trip, title="Walk")


async def test_adding_a_place_to_the_plan_syncs_both_and_reaches_the_feed(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    scope = f"plan:{trip.plan_id}"
    _, cursor, _ = await pull_all(api, bea, scope)
    place = await save_place(api, bea, trip, maps_url=GOOGLE)
    planned = await ok(
        await api.post(
            trip.path(f"/places/{place['id']}/add-to-plan"),
            json={"day": "2027-03-21"},
            headers=owner.headers,
        ),
        201,
    )
    assert (planned["title"], planned["place_id"]) == ("Senso-ji", place["id"])
    removed = await api.delete(trip.path(f"/itinerary/{planned['id']}"), headers=owner.headers)
    assert removed.status_code == 204

    items, _, _ = await pull_all(api, bea, scope, cursor)
    latest = {(i["entity_type"], i["entity_id"]): i for i in items}
    # The item using it was deleted, so the place is back on the shortlist.
    assert latest[("place", place["id"])]["data"]["status"] == "shortlist"
    assert latest[("itinerary_item", planned["id"])]["operation"] == "delete"
    feed = [i["data"] for i in items if i["entity_type"] == "activity_event"]
    assert [event["type"] for event in feed] == ["place.saved", "place.added_to_plan"]
    assert feed[1]["summary"] == {"item_id": planned["id"], "day": "2027-03-21"}


async def test_answers_are_only_ever_your_own(
    api: httpx.AsyncClient, trip: FinancePlan, live_settings: Settings
) -> None:
    item = await add_item(api, trip.owner, trip, title="Dinner")
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute(
            "SELECT set_config('app.actor_id', %s, true)", (trip.members["Bea"].user_id,)
        )
        connection.execute(
            "INSERT INTO schedule_places.item_attendance (item_id, plan_id, participant_id, "
            "status, created_at, updated_at) VALUES (%s, %s, %s, 'going', now(), now())",
            (item["id"], trip.plan_id, trip.people["Dan"]),
        )


async def test_another_trip_cannot_be_planned_into(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    first = await finance_plan(api, identity_provider, admin)
    other = await finance_plan(api, identity_provider, admin)
    place = await save_place(api, other.owner, other)
    stranger = await sign_in(api, identity_provider, name="Stranger")
    assert (await api.get(other.path("/places"), headers=stranger.headers)).status_code == 404
    borrowed = await api.post(
        first.path("/itinerary"),
        json={"title": "x", "place_id": place["id"]},
        headers=first.owner.headers,
    )
    assert borrowed.status_code == 422


async def budget_plans(api: httpx.AsyncClient, trip: FinancePlan) -> tuple[int, int, int]:
    """Spend so far, committed, and estimated, in the trip's base currency."""

    overview = await ok(await api.get(trip.path("/budgets"), headers=trip.owner.headers))
    total = overview["total"]
    return total["actual_minor"], total["committed_minor"], total["estimated_minor"]


async def test_editing_a_costed_item_keeps_one_cost_in_the_budget(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, ann = trip.owner, trip.people["Ann"]
    cost = {"currency": "USD", "amount_minor": 4_000, "category": "food"}
    dinner = await add_item(api, owner, trip, title="Dinner", day="2027-03-22", estimated_cost=cost)
    path = trip.path(f"/itinerary/{dinner['id']}")
    assert await budget_plans(api, trip) == (0, 0, 4_000)
    # Rename and move it (the cost follows the title), then mark it done with the same cost.
    moved = await ok(
        await api.put(
            path,
            json={"title": "Izakaya", "day": "2027-03-23", "estimated_cost": cost},
            headers=if_match(1, owner),
        )
    )
    commitment = moved["commitment_id"]

    async def commitment_row() -> tuple[str, int, str]:
        rows = await ok(await api.get(trip.path("/commitments"), headers=owner.headers))
        [row] = [row for row in rows if row["id"] == commitment]
        return row["description"], row["version"], row["state"]

    assert await commitment_row() == ("Izakaya", 2, "estimated")
    # Same cost, category omitted (kept): nothing in finance changes, so the finance
    # switch does not stand in the way.
    monkeypatch.setattr(live_settings, "finance_writes_enabled", False)
    done = await ok(
        await api.put(
            path,
            json={
                "title": "Izakaya",
                "day": "2027-03-23",
                "status": "done",
                "estimated_cost": {"currency": "USD", "amount_minor": 4_000},
            },
            headers=if_match(2, owner),
        )
    )
    assert done["commitment_id"] == commitment
    assert await commitment_row() == ("Izakaya", 2, "estimated")
    monkeypatch.setattr(live_settings, "finance_writes_enabled", True)
    assert await budget_plans(api, trip) == (0, 0, 4_000)
    # Cancelled while still only planned: the cost leaves the budget.
    await ok(
        await api.put(
            path,
            json={"title": "Izakaya", "day": "2027-03-23", "status": "cancelled"},
            headers=if_match(3, owner),
        )
    )
    assert await commitment_row() == ("Izakaya", 3, "cancelled")
    assert await budget_plans(api, trip) == (0, 0, 0)
    # An expense that names an item's cost counts once, as the expense.
    lunch = await add_item(api, owner, trip, title="Lunch", estimated_cost=cost)
    await add_expense(
        api, owner, trip, equal_expense(3_000, ann, [ann], commitment_id=lunch["commitment_id"])
    )
    assert await budget_plans(api, trip) == (3_000, 0, 0)


async def test_a_paid_cost_of_a_deleted_item_does_not_come_back(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, ann = trip.owner, trip.people["Ann"]
    museum = await add_item(
        api, owner, trip, title="Museum", estimated_cost={"currency": "USD", "amount_minor": 900}
    )
    paid = await add_expense(
        api, owner, trip, equal_expense(900, ann, [ann], commitment_id=museum["commitment_id"])
    )
    assert (
        await api.delete(trip.path(f"/itinerary/{museum['id']}"), headers=owner.headers)
    ).status_code == 204
    voided = await api.post(trip.path(f"/expenses/{paid['id']}/void"), headers=if_match(1, owner))
    assert voided.status_code == 200, voided.text
    rows = await ok(await api.get(trip.path("/commitments"), headers=owner.headers))
    assert [(row["id"], row["state"]) for row in rows] == [(museum["commitment_id"], "cancelled")]
    assert await budget_plans(api, trip) == (0, 0, 0)


async def test_keys_order_by_bytes_and_bad_input_is_refused(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner = trip.owner
    for key, title in (("a", "Third"), ("B", "First"), ("k", "Fourth"), ("V", "Second")):
        await add_item(api, owner, trip, title=title, day="2027-03-24", order_key=key)
    last = await add_item(api, owner, trip, title="Fifth", day="2027-03-24")
    assert last["order_key"] > "k"
    listed = await ok(await api.get(trip.path("/itinerary"), headers=owner.headers))
    assert [row["title"] for row in listed] == ["First", "Second", "Third", "Fourth", "Fifth"]
    for body in (
        {"title": "x", "order_key": "A" * 65},
        {"title": "x", "day": "2027-03-24", "start_time": "10:00:00+07:00"},
    ):
        refused = await api.post(trip.path("/itinerary"), json=body, headers=owner.headers)
        assert refused.status_code == 422, body
    for url in ("javascript:alert(1)", "intent://maps#Intent;end"):
        refused = await api.post(
            trip.path("/places"), json={"name": "x", "maps_url": url}, headers=owner.headers
        )
        assert refused.status_code == 422, url
    nan = await save_place(
        api, owner, trip, maps_url="https://www.openstreetmap.org/?mlat=NaN&mlon=1"
    )
    assert nan["resolution_state"] == "pending"


async def test_who_may_change_items_and_when(
    api: httpx.AsyncClient, trip: FinancePlan, identity_provider: IdentityProviderStub
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    joined = await api.post(
        "/v1/invites/redeem",
        json={
            "token": (
                await ok(await api.post(trip.path("/invites"), json={}, headers=owner.headers), 201)
            )["token"],
            "display_name": "Gia",
        },
    )
    guest = signed_in_from(joined.json()["session"])
    mine = await add_item(api, guest, trip, title="Guest's pick")
    beas = await add_item(api, bea, trip, title="Bea's pick")
    body = {"title": "Renamed"}
    assert (
        await api.put(trip.path(f"/itinerary/{beas['id']}"), json=body, headers=if_match(1, dan))
    ).status_code == 403
    await ok(
        await api.put(trip.path(f"/itinerary/{mine['id']}"), json=body, headers=if_match(1, guest))
    )
    await ok(
        await api.put(trip.path(f"/itinerary/{beas['id']}"), json=body, headers=if_match(1, owner))
    )
    version = (await ok(await api.get(trip.path(), headers=owner.headers)))["version"]
    await ok(await api.delete(trip.path(), headers=if_match(version, owner)))
    frozen = await api.post(trip.path("/itinerary"), json={"title": "Late"}, headers=bea.headers)
    assert frozen.status_code == 403
