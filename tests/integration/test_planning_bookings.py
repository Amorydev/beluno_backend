"""Bookings: masked everywhere, revealed to the right people, priced once."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.modules.iam import rate_limits
from beluno.testkit.api_client import SignedIn
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

CODE = "HTL-7731-QX"
NOTES = "Door code 4471, ask for Mrs Sato"


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


def hotel(trip: FinancePlan, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "lodging",
        "title": "Ryokan Sato",
        "provider": "Booking site",
        "start_date": "2027-03-20",
        "start_time": "15:00:00",
        "start_timezone": "Asia/Tokyo",
        "end_date": "2027-03-23",
        "traveler_ids": [trip.people["Bea"]],
        "payment_note": "pay_at_property",
        "price": {"currency": "USD", "amount_minor": 60_000},
        "secrets": {"confirmation_code": CODE, "private_notes": NOTES},
        **extra,
    }


async def reveal(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, booking: dict[str, Any]
) -> httpx.Response:
    return await api.post(trip.path(f"/bookings/{booking['id']}/reveal"), headers=user.headers)


async def test_secrets_are_masked_everywhere_and_revealed_to_the_right_people(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase, live_settings: Settings
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    scope = f"plan:{trip.plan_id}"
    _, cursor, _ = await pull_all(api, dan, scope)
    booking = await ok(
        await api.post(trip.path("/bookings"), json=hotel(trip), headers=owner.headers), 201
    )
    assert (booking["has_confirmation_code"], booking["has_private_notes"]) == (True, True)
    listed = await api.get(trip.path("/bookings"), headers=dan.headers)
    items, _, _ = await pull_all(api, dan, scope, cursor)
    for leaked in (listed.text, str(items), str(booking)):
        assert CODE not in leaked and NOTES not in leaked
    stored = admin.fetch("SELECT confirmation_code, private_notes FROM bookings.booking_secrets")
    assert all(
        CODE.encode() not in bytes(blob) and NOTES.encode() not in bytes(blob) for blob in stored[0]
    )

    # The creator, a traveler, and an organiser see them; another member does not.
    shown = await ok(await reveal(api, owner, trip, booking))
    assert shown == {"confirmation_code": CODE, "private_notes": NOTES}
    traveler = await reveal(api, bea, trip, booking)
    assert traveler.status_code == 200 and traveler.headers["cache-control"] == "no-store"
    assert (await reveal(api, dan, trip, booking)).status_code == 403
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events "
            "WHERE action = 'planning.booking_revealed'"
        )
        == 2
    )
    # Nor can Dan read the sealed row through the API role.
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (dan.user_id,))
        assert connection.execute("SELECT count(*) FROM bookings.booking_secrets").fetchone() == (
            0,
        )
    await ok(
        await api.patch(
            trip.path(f"/participants/{trip.people['Dan']}"),
            json={"role": "admin"},
            headers=if_match(1, owner),
        )
    )
    assert (await reveal(api, dan, trip, booking)).status_code == 200

    # Saving without secrets keeps them; null clears one.
    path = trip.path(f"/bookings/{booking['id']}")
    kept = await ok(await api.put(path, json=hotel(trip, secrets=None), headers=if_match(1, owner)))
    assert kept["has_confirmation_code"] is True
    cleared = await ok(
        await api.put(
            path,
            json=hotel(trip, secrets={"confirmation_code": None, "private_notes": NOTES}),
            headers=if_match(2, owner),
        )
    )
    assert (cleared["has_confirmation_code"], cleared["has_private_notes"]) == (False, True)
    assert await ok(await reveal(api, owner, trip, booking)) == {
        "confirmation_code": None,
        "private_notes": NOTES,
    }


async def test_a_booking_price_counts_once_and_a_paid_one_survives_cancelling(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner, ann = trip.owner, trip.people["Ann"]

    async def budget() -> tuple[int, int, int]:
        total = (await ok(await api.get(trip.path("/budgets"), headers=owner.headers)))["total"]
        return total["actual_minor"], total["committed_minor"], total["estimated_minor"]

    booking = await ok(
        await api.post(trip.path("/bookings"), json=hotel(trip), headers=owner.headers), 201
    )
    assert await budget() == (0, 0, 60_000)
    path = trip.path(f"/bookings/{booking['id']}")
    confirmed = await ok(
        await api.put(path, json=hotel(trip, status="confirmed"), headers=if_match(1, owner))
    )
    assert await budget() == (0, 60_000, 0)
    await add_expense(
        api,
        owner,
        trip,
        equal_expense(58_000, ann, [ann], commitment_id=confirmed["commitment_id"]),
    )
    assert await budget() == (58_000, 0, 0)
    await ok(await api.put(path, json=hotel(trip, status="cancelled"), headers=if_match(2, owner)))
    assert await budget() == (58_000, 0, 0)  # the money was spent; a refund goes on the expense

    scope = f"plan:{trip.plan_id}"
    items, _, _ = await pull_all(api, owner, scope)
    kinds = [
        (i["data"]["type"], i["data"]["summary"])
        for i in items
        if i["entity_type"] == "activity_event" and i["data"]["type"].startswith("booking.")
    ]
    assert kinds == [
        ("booking.added", {"booking_kind": "lodging"}),
        ("booking.confirmed", {"booking_kind": "lodging"}),
        ("booking.cancelled", {"booking_kind": "lodging"}),
    ]


async def test_items_link_bookings_and_bad_bookings_are_refused(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    trip: FinancePlan,
) -> None:
    owner = trip.owner
    booking = await ok(
        await api.post(
            trip.path("/bookings"), json=hotel(trip, secrets=None), headers=owner.headers
        ),
        201,
    )
    item = await ok(
        await api.post(
            trip.path("/itinerary"),
            json={"title": "Check in", "day": "2027-03-20", "booking_id": booking["id"]},
            headers=owner.headers,
        ),
        201,
    )
    assert item["booking_id"] == booking["id"]
    other = await finance_plan(api, identity_provider, admin)
    foreign = await api.post(
        other.path("/itinerary"),
        json={"title": "x", "booking_id": booking["id"]},
        headers=other.owner.headers,
    )
    assert foreign.status_code == 422
    for bad in (
        hotel(trip, start_timezone=None),  # a time without its zone
        hotel(trip, traveler_ids=[other.people["Ann"]]),  # someone from another trip
        hotel(trip, end_date="2027-03-01"),
    ):
        refused = await api.post(trip.path("/bookings"), json=bad, headers=owner.headers)
        assert refused.status_code == 422, bad


async def test_reveals_are_limited_and_secrets_need_a_keyring(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = trip.owner
    booking = await ok(
        await api.post(trip.path("/bookings"), json=hotel(trip), headers=owner.headers), 201
    )
    monkeypatch.setattr(
        rate_limits, "BOOKING_REVEAL_PER_USER", rate_limits.RateLimit("booking_reveal:test", 1, 600)
    )
    assert (await reveal(api, owner, trip, booking)).status_code == 200
    assert (await reveal(api, owner, trip, booking)).status_code == 429
    monkeypatch.setattr(live_settings, "booking_keys", None)
    refused = await api.post(trip.path("/bookings"), json=hotel(trip), headers=owner.headers)
    assert refused.status_code == 503
    await ok(
        await api.post(
            trip.path("/bookings"), json=hotel(trip, secrets=None), headers=owner.headers
        ),
        201,
    )


async def test_bookings_follow_their_people_and_keep_what_is_not_changed(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase, live_settings: Settings
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    booking = await ok(
        await api.post(
            trip.path("/bookings"),
            json=hotel(trip, traveler_ids=[trip.people["Cam"]]),
            headers=owner.headers,
        ),
        201,
    )
    path = trip.path(f"/bookings/{booking['id']}")
    # Bea turns out to be placeholder Cam: she claims it into her own participation.
    claim = await ok(
        await api.post(
            trip.path(f"/participants/{trip.people['Cam']}/claim-invites"),
            json={},
            headers=owner.headers,
        ),
        201,
    )
    await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": claim["token"], "merge_existing": True},
            headers=bea.headers,
        )
    )
    assert (await reveal(api, bea, trip, booking)).status_code == 200
    # Saving with the same (now merged) traveler still works; one secret changes alone.
    changed = await ok(
        await api.put(
            path,
            json=hotel(
                trip, traveler_ids=[trip.people["Cam"]], secrets={"private_notes": "Room 2"}
            ),
            headers=if_match(1, owner),
        )
    )
    assert (changed["has_confirmation_code"], changed["has_private_notes"]) == (True, True)
    assert await ok(await reveal(api, owner, trip, booking)) == {
        "confirmation_code": CODE,
        "private_notes": "Room 2",
    }
    # Only whoever added it, or an organiser, changes it: Dan neither by API nor in SQL.
    assert (await api.put(path, json=hotel(trip), headers=if_match(2, dan))).status_code == 403
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (dan.user_id,))
        connection.execute(
            "UPDATE bookings.bookings SET traveler_ids = array_append(traveler_ids, %s), "
            "version = version + 1 WHERE id = %s",
            (trip.people["Dan"], booking["id"]),
        )
    # An altered secret is refused as a whole, never half-read.
    admin.execute(
        "UPDATE bookings.booking_secrets SET confirmation_code = "
        "overlay(confirmation_code placing '\\x00'::bytea from 20 for 1) WHERE booking_id = %s",
        booking["id"],
    )
    unreadable = await reveal(api, owner, trip, booking)
    assert unreadable.status_code == 409
    assert unreadable.json()["code"] == "BOOKING_SECRETS_UNREADABLE"


async def test_deleting_a_booking_forgets_its_secrets_and_keeps_its_items_editable(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    owner, ann = trip.owner, trip.people["Ann"]
    booking = await ok(
        await api.post(
            trip.path("/bookings"), json=hotel(trip, status="confirmed"), headers=owner.headers
        ),
        201,
    )
    item = await ok(
        await api.post(
            trip.path("/itinerary"),
            json={"title": "Check in", "booking_id": booking["id"]},
            headers=owner.headers,
        ),
        201,
    )
    # Paid, cancelled, confirmed again, then the expense is voided: the cost is
    # committed again (it follows the booking), not lost.
    paid = await add_expense(
        api, owner, trip, equal_expense(60_000, ann, [ann], commitment_id=booking["commitment_id"])
    )
    path = trip.path(f"/bookings/{booking['id']}")
    await ok(await api.put(path, json=hotel(trip, status="cancelled"), headers=if_match(1, owner)))
    await ok(await api.put(path, json=hotel(trip, status="confirmed"), headers=if_match(2, owner)))
    await ok(await api.post(trip.path(f"/expenses/{paid['id']}/void"), headers=if_match(1, owner)))
    again = await ok(await api.get(path, headers=owner.headers))
    commitments = await ok(await api.get(trip.path("/commitments"), headers=owner.headers))
    assert [(c["id"], c["state"]) for c in commitments] == [(again["commitment_id"], "committed")]

    assert (await api.delete(path, headers=owner.headers)).status_code == 204
    assert admin.fetch(
        "SELECT confirmation_code, private_notes FROM bookings.booking_secrets "
        "WHERE booking_id = %s",
        booking["id"],
    ) == [(None, None)]
    kept = await ok(
        await api.put(
            trip.path(f"/itinerary/{item['id']}"),
            json={"title": "Check in late", "booking_id": booking["id"]},
            headers=if_match(1, owner),
        )
    )
    assert kept["booking_id"] == booking["id"]
