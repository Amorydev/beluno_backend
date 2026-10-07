"""Money settings (settled-under tolerance, personal spend) and ledger confirmations."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.testkit.api_client import SignedIn
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    ledger_balances,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def ledger(api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan) -> dict[str, Any]:
    response = await api.get(trip.path("/ledger"), headers=user.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def settle(
    api: httpx.AsyncClient, trip: FinancePlan, debtor: str, creditor: str, amount: int
) -> None:
    response = await api.post(
        trip.path("/settlements"),
        json={
            "from_participant_id": debtor,
            "to_participant_id": creditor,
            "currency": "USD",
            "amount_minor": amount,
            "occurred_on": "2026-10-07",
        },
        headers=trip.owner.headers,
    )
    assert response.status_code == 201, response.text


async def configure(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, **settings: Any
) -> httpx.Response:
    return await api.patch(trip.path("/ledger/settings"), json=settings, headers=user.headers)


async def test_small_base_currency_balances_count_as_settled_without_changing_postings(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    await add_expense(api, trip.owner, trip, equal_expense(1_000, ann, [ann, bea, cam]))
    await settle(api, trip, bea, ann, 330)
    await settle(api, trip, cam, ann, 333)
    exact = {(ann, "USD"): 3, (bea, "USD"): -3}
    assert await ledger_balances(api, trip.owner, trip) == exact
    before = await ledger(api, trip.owner, trip)
    assert (before["status"], before["settle_tolerance_minor"]) == ("open", 0)
    preview = await api.get(trip.path("/ledger/settlement-preview"), headers=trip.owner.headers)
    assert preview.json()[0]["transfers"] == [
        {"from_participant_id": bea, "to_participant_id": ann, "amount_minor": 3}
    ]
    # The ledger entity carries the same suggestions, so settling up works offline.
    assert before["suggestions"] == preview.json()

    member = await configure(api, trip.members["Bea"], trip, settle_tolerance_minor=5)
    assert member.status_code == 403
    for invalid in ({}, {"settle_tolerance_minor": -1}, {"settle_tolerance_minor": None}):
        assert (await configure(api, trip.owner, trip, **invalid)).status_code == 422
    configured = await configure(api, trip.owner, trip, settle_tolerance_minor=5)
    assert configured.status_code == 200, configured.text
    body = configured.json()
    assert (body["status"], body["settle_tolerance_minor"]) == ("settled", 5)
    assert body["version"] == before["version"] + 1
    assert await ledger_balances(api, trip.owner, trip) == exact
    preview = await api.get(trip.path("/ledger/settlement-preview"), headers=trip.owner.headers)
    assert preview.json()[0]["transfers"] == []
    assert body["suggestions"] == preview.json()

    # The tolerance is in the base currency; other currencies still settle exactly.
    await add_expense(
        api, trip.owner, trip, equal_expense(4, ann, [ann, bea], currency="JPY", description="Gum")
    )
    assert (await ledger(api, trip.owner, trip))["status"] == "reopened"


async def test_budgets_can_leave_out_personal_spend(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(1_000, ann, [ann, bea]))
    await add_expense(api, trip.owner, trip, equal_expense(400, bea, [bea], description="Socks"))

    async def actual() -> int:
        overview = await api.get(trip.path("/budgets"), headers=trip.owner.headers)
        total: int = overview.json()["total"]["actual_minor"]
        return total

    assert await actual() == 1_400
    assert (await ledger(api, trip.owner, trip))["count_personal_spend"] is True
    configured = await configure(api, trip.owner, trip, count_personal_spend=False)
    assert configured.status_code == 200 and configured.json()["count_personal_spend"] is False
    assert await actual() == 1_000


async def test_confirmations_go_stale_with_the_next_entry(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    bea_user = trip.members["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(1_000, ann, [ann, bea]))
    seq = (await ledger(api, trip.owner, trip))["ledger_seq"]

    async def confirm(user: SignedIn, ledger_seq: int) -> httpx.Response:
        return await api.post(
            trip.path("/ledger/confirm"), json={"ledger_seq": ledger_seq}, headers=user.headers
        )

    first = await confirm(trip.owner, seq)
    assert first.status_code == 200, first.text
    assert [row["participant_id"] for row in first.json()["confirmations"]] == [ann]
    repeat = await confirm(trip.owner, seq)
    assert repeat.json()["version"] == first.json()["version"]
    stale = await confirm(bea_user, seq - 1)
    assert stale.status_code == 409 and stale.json()["code"] == "LEDGER_CHANGED"
    both = await confirm(bea_user, seq)
    assert {row["participant_id"] for row in both.json()["confirmations"]} == {ann, bea}

    await add_expense(api, trip.owner, trip, equal_expense(200, bea, [ann, bea]))
    after = await ledger(api, trip.owner, trip)
    assert (after["ledger_seq"], after["confirmations"]) == (seq + 1, [])
