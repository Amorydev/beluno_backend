"""A tenant holding one of every release-1 entity, for isolation sweeps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, exercise_money_and_members
from beluno.testkit.identity import IdentityProviderStub


@dataclass
class FullTenant:
    trip: FinancePlan
    # Path parameter name (``plan_id``, ``expense_id``, ...) -> an ID the tenant owns.
    ids: dict[str, str]


async def full_tenant(
    api: httpx.AsyncClient, provider: IdentityProviderStub, admin: AdminDatabase
) -> FullTenant:
    """A used trip (see ``exercise_money_and_members``) plus a commitment, kitty
    settings, a ledger confirmation, join and claim links, and a crew."""

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
        },
    )
