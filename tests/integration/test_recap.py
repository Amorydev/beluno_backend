"""A recap adds up the trip the way the budget screen does, and its card stays public-safe."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, add_expense, equal_expense, finance_plan, if_match
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def recap(api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan) -> dict[str, Any]:
    body: dict[str, Any] = await ok(await api.get(trip.path("/recap"), headers=user.headers))
    return body


async def test_the_recap_counts_spending_plans_decisions_and_settling(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann, bea_id, cam, dan_id = (trip.people[n] for n in ("Ann", "Bea", "Cam", "Dan"))
    await ok(
        await api.patch(
            trip.path(),
            json={
                "timing": {"mode": "date", "start_date": "2027-03-18", "end_date": "2027-03-27"},
                "destinations": [
                    {
                        "name": "Tokyo",
                        "code": "TYO",
                        "start_date": "2027-03-18",
                        "end_date": "2027-03-21",
                    },
                    {"name": "Kyoto"},
                ],
            },
            headers=if_match(1, owner),
        )
    )
    food = equal_expense(1_000, ann, [ann, bea_id], description="Ramen")
    await add_expense(api, owner, trip, food)
    stay = {**equal_expense(3_000, bea_id, [ann, bea_id, cam, dan_id]), "category": "lodging"}
    await add_expense(api, bea, trip, stay)

    place = await ok(
        await api.post(trip.path("/places"), json={"name": "Fushimi Inari"}, headers=owner.headers),
        201,
    )
    other = await ok(
        await api.post(trip.path("/places"), json={"name": "Arcade"}, headers=owner.headers), 201
    )
    for user, wanted in ((bea, place), (dan, place), (owner, other)):
        await ok(
            await api.put(
                trip.path(f"/places/{wanted['id']}/reaction"),
                json={"wants": True},
                headers=user.headers,
            )
        )
    for status in ("planned", "done", "cancelled"):
        await ok(
            await api.post(
                trip.path("/itinerary"),
                json={"title": f"Item {status}", "status": status},
                headers=owner.headers,
            ),
            201,
        )
    poll = await ok(
        await api.post(
            trip.path("/polls"),
            json={"question": "Where?", "options": [{"label": "A"}, {"label": "B"}]},
            headers=owner.headers,
        ),
        201,
    )
    await ok(
        await api.put(
            trip.path(f"/polls/{poll['id']}/vote"),
            json={"option_id": poll["options"][0]["id"]},
            headers=owner.headers,
        )
    )
    await ok(await api.post(trip.path(f"/polls/{poll['id']}/close"), headers=owner.headers))

    summary = await recap(api, dan, trip)
    assert (summary["days"], summary["people"], summary["currency"]) == (10, 4, "USD")
    assert summary["stops"] == [
        {"name": "Tokyo", "code": "TYO", "nights": 3},
        {"name": "Kyoto", "code": None, "nights": None},
    ]
    assert (summary["spent_minor"], summary["per_person_per_day_minor"]) == (4_000, 100)
    assert summary["unconverted"] is False
    assert summary["categories"] == [
        {"category": "lodging", "spent_minor": 3_000, "share_basis_points": 7_500},
        {"category": "food", "spent_minor": 1_000, "share_basis_points": 2_500},
    ]
    assert summary["top_place"] == {
        "place_id": place["id"],
        "name": "Fushimi Inari",
        "wanted_by": 2,
        "of_people": 3,
    }
    assert (summary["itinerary_done"], summary["itinerary_total"]) == (1, 2)
    assert summary["polls_decided"] == 1
    assert {row["active"] for row in summary["crew"]} == {True}
    settled = {row["participant_id"]: row["settled"] for row in summary["crew"]}
    assert settled == {ann: False, bea_id: False, cam: False, dan_id: False}
    assert (summary["all_settled"], summary["settled_on"]) == (False, None)
    assert summary["share"] == {
        "route": ["Tokyo", "Kyoto"],
        "start_date": "2027-03-18",
        "days": 10,
        "people": 4,
        "currency": "USD",
        "spent_minor": 4_000,
        "spent_complete": True,
    }

    # Ann owes 250, Cam and Dan 750 each, all to Bea.
    for debtor, amount, day in (
        (ann, 250, "2027-03-28"),
        (cam, 750, "2027-04-02"),
        (dan_id, 750, "2027-03-30"),
    ):
        await ok(
            await api.post(
                trip.path("/settlements"),
                json={
                    "from_participant_id": debtor,
                    "to_participant_id": bea_id,
                    "currency": "USD",
                    "amount_minor": amount,
                    "occurred_on": day,
                },
                headers=owner.headers,
            ),
            201,
        )
    done = await recap(api, owner, trip)
    assert all(row["settled"] for row in done["crew"])
    assert (done["all_settled"], done["settled_on"]) == (True, "2027-04-02")


async def test_the_recap_is_for_people_in_the_plan_and_holds_no_secret(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, trip: FinancePlan
) -> None:
    owner = trip.owner
    await ok(
        await api.post(
            trip.path("/bookings"),
            json={
                "kind": "lodging",
                "title": "Ryokan",
                "secrets": {"confirmation_code": "RYO-1234", "private_notes": "Door 4471"},
            },
            headers=owner.headers,
        ),
        201,
    )
    response = await api.get(trip.path("/recap"), headers=trip.members["Bea"].headers)
    assert response.status_code == 200, response.text
    assert "RYO-1234" not in response.text and "Door 4471" not in response.text
    empty = response.json()
    assert (empty["days"], empty["per_person_per_day_minor"], empty["top_place"]) == (
        None,
        None,
        None,
    )
    # Nothing was owed and nothing settled: the ledger is open, so not "settled".
    assert (empty["spent_minor"], empty["categories"], empty["all_settled"]) == (0, [], False)

    stranger = await sign_in(api, identity_provider, name="Stranger")
    assert (await api.get(trip.path("/recap"), headers=stranger.headers)).status_code == 404


async def test_a_hangout_counts_its_local_days(api: httpx.AsyncClient, trip: FinancePlan) -> None:
    hangout = await ok(
        await api.post(
            "/v1/plans",
            json={
                "type": "hangout",
                "title": "Karaoke",
                "base_currency": "VND",
                "timing": {
                    "mode": "datetime",
                    "starts_at": "2027-03-05T19:00:00+07:00",
                    "ends_at": "2027-03-06T00:00:00+07:00",
                    "timezone": "Asia/Ho_Chi_Minh",
                },
            },
            headers=trip.owner.headers,
        ),
        201,
    )
    summary = await ok(
        await api.get(f"/v1/plans/{hangout['id']}/recap", headers=trip.owner.headers)
    )
    # 19:00 to midnight is one evening, and the card can still show its month.
    assert (summary["days"], summary["people"], summary["stops"]) == (1, 1, [])
    assert (summary["start_date"], summary["end_date"]) == ("2027-03-05", "2027-03-05")
    assert (summary["share"]["route"], summary["share"]["start_date"]) == ([], "2027-03-05")


async def test_settled_follows_the_ledger_and_counts_people_who_left(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, dan = trip.owner, trip.members["Dan"]
    ann, bea_id, dan_id = (trip.people[n] for n in ("Ann", "Bea", "Dan"))
    configured = await api.patch(
        trip.path("/ledger/settings"), json={"settle_tolerance_minor": 100}, headers=owner.headers
    )
    assert configured.status_code == 200, configured.text
    place = await ok(
        await api.post(trip.path("/places"), json={"name": "Onsen"}, headers=owner.headers), 201
    )
    for user in (owner, dan):
        await ok(
            await api.put(
                trip.path(f"/places/{place['id']}/reaction"),
                json={"wants": True},
                headers=user.headers,
            )
        )
    # Dan leaves owing 200 USD (over the tolerance); 100 EUR with no rate leaves Bea
    # owing 50 EUR, which a USD tolerance never covers.
    await add_expense(api, owner, trip, equal_expense(400, ann, [ann, dan_id]))
    await add_expense(api, owner, trip, equal_expense(100, ann, [ann, bea_id], currency="EUR"))
    left = await api.post(trip.path("/leave"), headers=dan.headers)
    assert left.status_code == 204, left.text

    summary = await recap(api, owner, trip)
    crew = {row["participant_id"]: (row["active"], row["settled"]) for row in summary["crew"]}
    assert crew[dan_id] == (False, False)
    assert crew[bea_id] == (True, False)
    assert (summary["all_settled"], summary["settled_on"]) == (False, None)
    assert summary["unconverted"] is True and summary["share"]["spent_complete"] is False
    assert summary["spent_minor"] == 400
    assert summary["top_place"]["wanted_by"] == 1
