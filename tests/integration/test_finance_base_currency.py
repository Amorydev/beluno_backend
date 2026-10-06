"""Changing the base currency: originals stay, base values read through the change chain."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from beluno.modules.finance.fx import convert
from beluno.testkit.api_client import SignedIn
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    ledger_balances,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def move_base(
    api: httpx.AsyncClient,
    user: SignedIn,
    trip: FinancePlan,
    version: int,
    currency: str,
    rate: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"currency": currency}
    if rate is not None:
        body["rate"] = {"rate": rate}
    return await api.post(trip.path("/base-currency"), json=body, headers=if_match(version, user))


async def plan_version(api: httpx.AsyncClient, trip: FinancePlan) -> int:
    version: int = (await api.get(trip.path(), headers=trip.owner.headers)).json()["version"]
    return version


async def actual_total(api: httpx.AsyncClient, trip: FinancePlan) -> tuple[str, int, int]:
    overview = (await api.get(trip.path("/budgets"), headers=trip.owner.headers)).json()
    return (
        overview["currency"],
        overview["total"]["actual_minor"],
        overview["total"]["committed_minor"],
    )


async def test_a_plan_without_money_just_switches(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    patched = await api.patch(
        trip.path(), json={"base_currency": "EUR"}, headers=if_match(1, trip.owner)
    )
    assert patched.status_code == 422
    moved = await move_base(api, trip.owner, trip, 1, "EUR")
    assert moved.status_code == 200, moved.text
    assert (moved.json()["base_currency"], moved.json()["version"]) == ("EUR", 2)
    assert (await move_base(api, trip.owner, trip, 2, "EUR")).status_code == 409
    assert (await move_base(api, trip.owner, trip, 2, "XYZ")).status_code == 422
    ledger = await api.get(trip.path("/ledger"), headers=trip.owner.headers)
    assert ledger.json()["base_changes"] == []


async def test_base_values_follow_the_chain_and_originals_never_change(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    owner = trip.owner
    dollars = await add_expense(api, owner, trip, equal_expense(1_000, ann, [ann, bea]))
    yen = await add_expense(
        api,
        owner,
        trip,
        equal_expense(3_000, bea, [ann, bea], currency="JPY", base_rate={"rate": "0.0067"}),
    )
    assert yen["revision"]["base"]["amount_minor"] == 2_010
    commitment = await api.post(
        trip.path("/commitments"),
        json={
            "description": "Ryokan",
            "currency": "JPY",
            "amount_minor": 10_000,
            "state": "committed",
            "base_rate": {"rate": "0.0067"},
        },
        headers=owner.headers,
    )
    assert commitment.status_code == 201, commitment.text
    budget = await api.post(
        trip.path("/budgets"), json={"scope": "total", "limit_minor": 10_000}, headers=owner.headers
    )
    configured = await api.patch(
        trip.path("/ledger/settings"), json={"settle_tolerance_minor": 50}, headers=owner.headers
    )
    assert configured.status_code == 200
    balances = await ledger_balances(api, owner, trip)
    assert await actual_total(api, trip) == ("USD", 3_010, 6_700)

    version = await plan_version(api, trip)
    assert (await move_base(api, owner, trip, version, "EUR")).status_code == 422
    assert (
        await move_base(api, trip.members["Bea"], trip, version, "EUR", "0.9")
    ).status_code == 403
    assert (await move_base(api, owner, trip, version - 1, "EUR", "0.9")).status_code == 412
    moved = await move_base(api, owner, trip, version, "EUR", "0.9")
    assert moved.status_code == 200, moved.text
    assert moved.json()["base_currency"] == "EUR"

    # Postings, balances, and every original amount and snapshot stay as they were.
    assert await ledger_balances(api, owner, trip) == balances
    for original in (dollars, yen):
        now = await api.get(trip.path(f"/expenses/{original['id']}"), headers=owner.headers)
        assert now.json()["revision"] == original["revision"]
    ledger = (await api.get(trip.path("/ledger"), headers=owner.headers)).json()
    assert [
        (row["number"], row["from_currency"], row["to_currency"], row["rate"])
        for row in ledger["base_changes"]
    ] == [(1, "USD", "EUR", "0.9")]
    assert ledger["settle_tolerance_minor"] == 45
    rebased = (await api.get(trip.path("/budgets"), headers=owner.headers)).json()["budgets"][0]
    assert (rebased["budget"]["currency"], rebased["budget"]["limit_minor"]) == ("EUR", 9_000)
    assert rebased["budget"]["version"] == budget.json()["version"] + 1

    def through_chain(amount: int, *rates: str) -> int:
        for rate in rates:
            amount = convert(amount, from_exponent=2, to_exponent=2, rate=Decimal(rate))
        return amount

    yen_in_dollars = convert(3_000, from_exponent=0, to_exponent=2, rate=Decimal("0.0067"))
    ryokan_in_dollars = convert(10_000, from_exponent=0, to_exponent=2, rate=Decimal("0.0067"))
    assert await actual_total(api, trip) == (
        "EUR",
        through_chain(1_000, "0.9") + through_chain(yen_in_dollars, "0.9"),
        through_chain(ryokan_in_dollars, "0.9"),
    )

    # Later entries are valued in the new base and carry the change number.
    euros = await add_expense(api, owner, trip, equal_expense(500, ann, [ann, bea], currency="EUR"))
    assert (euros["revision"]["base_change_number"], euros["revision"]["base"]["currency"]) == (
        1,
        "EUR",
    )
    # Moving back: amounts in today's base count as they are, others go through the chain.
    back = await move_base(api, owner, trip, await plan_version(api, trip), "USD", "1.1")
    assert back.status_code == 200, back.text
    assert await actual_total(api, trip) == (
        "USD",
        1_000 + through_chain(yen_in_dollars, "0.9", "1.1") + through_chain(500, "1.1"),
        through_chain(ryokan_in_dollars, "0.9", "1.1"),
    )


async def test_an_open_consolidation_holds_the_base_currency(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(3_000, ann, [ann, bea], currency="JPY"))
    consolidated = await api.post(
        trip.path("/ledger/consolidations"),
        json={"rates": [{"currency": "JPY", "rate": "0.0067"}]},
        headers=trip.owner.headers,
    )
    assert consolidated.status_code == 201, consolidated.text
    version = await plan_version(api, trip)
    held = await move_base(api, trip.owner, trip, version, "EUR", "0.9")
    assert held.status_code == 409 and held.json()["code"] == "CONSOLIDATION_OPEN"
    undone = await api.post(
        trip.path(f"/ledger/consolidations/{consolidated.json()['id']}/reverse"),
        headers=if_match(1, trip.owner),
    )
    assert undone.status_code == 200
    assert (await move_base(api, trip.owner, trip, version, "EUR", "0.9")).status_code == 200
