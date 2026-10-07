"""A tenant holding one of every release-1 entity, for isolation sweeps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, exercise_money_and_members
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.passkeys import SoftAuthenticator


@dataclass
class FullTenant:
    trip: FinancePlan
    # Path parameter name (``plan_id``, ``expense_id``, ...) -> an ID the tenant owns.
    ids: dict[str, str]


async def full_tenant(
    api: httpx.AsyncClient, provider: IdentityProviderStub, admin: AdminDatabase
) -> FullTenant:
    """A used trip (see ``exercise_money_and_members``) plus a commitment, kitty
    settings, a ledger confirmation, join and claim links, a crew, a wanted place, an
    itinerary item with a cost and an answer, a decided poll, a booking with sealed
    secrets, a done task, packing items (a shared template, a private item), and a
    problem report with diagnostics, a passkey, and a receipt awaiting upload."""

    trip = await exercise_money_and_members(api, provider, admin)
    owner = trip.owner

    async def created(path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await api.post(path, json=body, headers=owner.headers)
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()
        return result

    async def listed(path: str) -> list[dict[str, Any]]:
        response = await api.get(trip.path(path), headers=owner.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        rows: list[dict[str, Any]] = body["items"] if isinstance(body, dict) else body
        assert rows, path
        return rows

    commitment = await created(
        trip.path("/commitments"),
        {"category": "lodging", "description": "Cabin", "currency": "EUR", "amount_minor": 900},
    )
    await created(trip.path(f"/participants/{trip.people['Cam']}/claim-invites"), {})
    crew = await created(
        "/v1/crews", {"name": "Trip crew", "member_user_ids": [trip.members["Dan"].user_id]}
    )
    fund = await api.put(
        trip.path("/fund"),
        json={"custodian_participant_id": trip.people["Dan"]},
        headers=owner.headers,
    )
    assert fund.status_code == 200, fund.text
    seq = (await api.get(trip.path("/ledger"), headers=owner.headers)).json()["ledger_seq"]
    confirmed = await api.post(
        trip.path("/ledger/confirm"), json={"ledger_seq": seq}, headers=owner.headers
    )
    assert confirmed.status_code == 200, confirmed.text
    place = await created(trip.path("/places"), {"name": "Senso-ji", "category": "sight"})
    reacted = await api.put(
        trip.path(f"/places/{place['id']}/reaction"), json={"wants": True}, headers=owner.headers
    )
    assert reacted.status_code == 200, reacted.text
    stop = await created(
        trip.path("/itinerary"),
        {
            "title": "Temple",
            "day": "2027-03-21",
            "place_id": place["id"],
            "estimated_cost": {"currency": "EUR", "amount_minor": 500},
        },
    )
    going = await api.put(
        trip.path(f"/itinerary/{stop['id']}/attendance"),
        json={"status": "going"},
        headers=owner.headers,
    )
    assert going.status_code == 200, going.text
    booking = await created(
        trip.path("/bookings"),
        {
            "kind": "lodging",
            "title": "Ryokan",
            "place_id": place["id"],
            "traveler_ids": [trip.people["Dan"]],
            "price": {"currency": "EUR", "amount_minor": 1_000},
            "secrets": {"confirmation_code": "RYO-1", "private_notes": "Late check-in"},
        },
    )
    poll = await created(
        trip.path("/polls"),
        {
            "question": "Where first?",
            "options": [{"label": "Temple", "place_id": place["id"]}, {"label": "Market"}],
        },
    )
    voted = await api.put(
        trip.path(f"/polls/{poll['id']}/vote"),
        json={"option_id": poll["options"][0]["id"]},
        headers=owner.headers,
    )
    assert voted.status_code == 200, voted.text
    closed = await api.post(trip.path(f"/polls/{poll['id']}/close"), headers=owner.headers)
    assert closed.status_code == 200, closed.text
    applied = await api.post(
        trip.path(f"/polls/{poll['id']}/outcome"),
        json={"action": "save_place"},
        headers=owner.headers,
    )
    assert applied.status_code == 200, applied.text
    task = await created(
        trip.path("/tasks"),
        {
            "title": "Print the ryokan voucher",
            "assignee_participant_id": trip.people["Dan"],
            "booking_id": booking["id"],
            "due_date": "2027-03-19",
            "status": "done",
        },
    )
    template = await api.post(
        trip.path("/packing/templates"),
        json={"template_id": "onsen", "items": [{"name": "Yukata", "category": "clothes"}]},
        headers=owner.headers,
    )
    assert template.status_code == 200, template.text
    await created(trip.path("/packing"), {"name": "Earplugs", "visibility": "private"})
    receipt = await created(
        trip.path("/media"),
        {
            "kind": "receipt",
            "content_type": "image/jpeg",
            "size_bytes": 1024,
            "expense_id": next(
                row["id"] for row in await listed("/expenses") if row["state"] == "active"
            ),
        },
    )
    passkey_options = await api.post("/v1/me/passkeys/registration-options", headers=owner.headers)
    assert passkey_options.status_code == 200, passkey_options.text
    passkey = await api.post(
        "/v1/me/passkeys",
        json={
            "challenge_id": passkey_options.json()["challenge_id"],
            "credential": SoftAuthenticator().create(passkey_options.json()["public_key"]),
            "label": "Phone",
        },
        headers=owner.headers,
    )
    assert passkey.status_code == 201, passkey.text
    reported = await api.post(
        "/v1/support/reports",
        json={
            "category": "balance_wrong",
            "plan_id": trip.plan_id,
            "linked": {"entity_type": "booking", "entity_id": booking["id"]},
            "message": "My share looks off by one",
        },
        headers=owner.headers,
    )
    assert reported.status_code == 201, reported.text
    budgets = (await api.get(trip.path("/budgets"), headers=owner.headers)).json()["budgets"]
    assert budgets
    return FullTenant(
        trip=trip,
        ids={
            "plan_id": trip.plan_id,
            "participant_id": trip.people["Dan"],
            "expense_id": (await listed("/expenses"))[0]["id"],
            "settlement_id": (await listed("/settlements"))[0]["id"],
            "budget_id": budgets[0]["budget"]["id"],
            "commitment_id": commitment["id"],
            "consolidation_id": (await listed("/ledger/consolidations"))[0]["id"],
            "invite_id": (await listed("/invites"))[0]["id"],
            "crew_id": crew["id"],
            "session_id": owner.session_id,
            "place_id": place["id"],
            "item_id": stop["id"],
            "poll_id": poll["id"],
            "booking_id": booking["id"],
            "task_id": task["id"],
            "packing_item_id": template.json()["items"][0]["id"],
            "passkey_id": passkey.json()["id"],
            "media_id": receipt["id"],
        },
    )
