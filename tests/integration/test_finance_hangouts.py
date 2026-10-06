"""Hangouts split costs and settle up; budgets, planned costs, and the kitty are for trips."""

from __future__ import annotations

import httpx
import pytest

from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    ledger_balances,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def test_hangouts_keep_expenses_and_settlements_but_refuse_trip_money_tools(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    hangout = await finance_plan(api, identity_provider, admin, plan_type="hangout")
    ann, bea, cam = (hangout.people[name] for name in ("Ann", "Bea", "Cam"))
    owner = hangout.owner
    expense = await add_expense(api, owner, hangout, equal_expense(900, ann, [ann, bea, cam]))
    refunded = await api.post(
        hangout.path(f"/expenses/{expense['id']}/refunds"),
        json={"amount_minor": 300, "recipient": {"participant_id": ann}},
        headers=if_match(1, owner),
    )
    assert refunded.status_code == 200, refunded.text
    paid = await api.post(
        hangout.path("/settlements"),
        json={
            "from_participant_id": bea,
            "to_participant_id": ann,
            "currency": "USD",
            "amount_minor": 200,
            "occurred_on": "2026-10-06",
        },
        headers=owner.headers,
    )
    assert paid.status_code == 201, paid.text
    assert await ledger_balances(api, owner, hangout) == {(ann, "USD"): 200, (cam, "USD"): -200}

    refused = {
        "budget": await api.post(
            hangout.path("/budgets"),
            json={"scope": "total", "limit_minor": 10_000},
            headers=owner.headers,
        ),
        "commitment": await api.post(
            hangout.path("/commitments"),
            json={"description": "Karaoke room", "currency": "USD", "amount_minor": 5_000},
            headers=owner.headers,
        ),
        "fund settings": await api.put(
            hangout.path("/fund"), json={"custodian_participant_id": ann}, headers=owner.headers
        ),
        "contribution": await api.post(
            hangout.path("/fund/contributions"),
            json={
                "participant_id": ann,
                "currency": "USD",
                "amount_minor": 1_000,
                "occurred_on": "2026-10-06",
            },
            headers=owner.headers,
        ),
        "fund-paid expense": await api.post(
            hangout.path("/expenses"),
            json={
                **equal_expense(300, ann, [ann, bea]),
                "payers": [{"fund": True, "amount_minor": 300}],
            },
            headers=owner.headers,
        ),
        "refund to the fund": await api.post(
            hangout.path(f"/expenses/{expense['id']}/refunds"),
            json={"amount_minor": 100, "recipient": {"fund": True}},
            headers=if_match(2, owner),
        ),
    }
    for name, response in refused.items():
        assert response.status_code == 409, (name, response.text)
        assert response.json()["code"] == "NOT_AVAILABLE_FOR_HANGOUT", name

    # Reads stay available and empty.
    budgets = await api.get(hangout.path("/budgets"), headers=owner.headers)
    assert budgets.status_code == 200 and budgets.json()["budgets"] == []
