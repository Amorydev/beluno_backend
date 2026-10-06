"""Kitty targets per member, contributions against them, and cash counts."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import finance_plan, if_match, pull_all
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


def movement(participant_id: str, amount: int) -> dict[str, Any]:
    return {
        "participant_id": participant_id,
        "currency": "JPY",
        "amount_minor": amount,
        "occurred_on": "2027-03-20",
    }


async def test_targets_contributions_and_counts(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    owner, custodian, member = trip.owner, trip.members["Bea"], trip.members["Dan"]
    target = {"currency": "JPY", "amount_minor": 10_000}
    settings = await api.put(
        trip.path("/fund"),
        json={"custodian_participant_id": bea, "target": target},
        headers=owner.headers,
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["target"] == target
    invalid = await api.put(
        trip.path("/fund"),
        json={"target": {"currency": "JPY", "amount_minor": 0}},
        headers=if_match(1, owner),
    )
    assert invalid.status_code == 422

    for user, person, amount in ((owner, ann, 5_000), (custodian, bea, 10_000)):
        contributed = await api.post(
            trip.path("/fund/contributions"), json=movement(person, amount), headers=user.headers
        )
        assert contributed.status_code == 201, contributed.text
    seq = (await api.get(trip.path("/ledger"), headers=owner.headers)).json()["ledger_seq"]

    async def count(user: Any, counted: int) -> httpx.Response:
        return await api.post(
            trip.path("/fund/counts"),
            json={"currency": "JPY", "counted_minor": counted, "note": "Envelope"},
            headers=user.headers,
        )

    assert (await count(member, 15_000)).status_code == 403
    assert (await count(custodian, -1)).status_code == 422
    short = await count(custodian, 14_000)
    assert short.status_code == 201, short.text
    assert (short.json()["expected_minor"], short.json()["difference_minor"]) == (15_000, -1_000)
    matched = await count(owner, 15_000)
    assert matched.json()["difference_minor"] == 0

    fund = (await api.get(trip.path("/fund"), headers=member.headers)).json()
    assert fund["settings"]["target"] == target
    assert {(row["participant_id"], row["contributed_minor"]) for row in fund["contributions"]} == {
        (ann, 5_000),
        (bea, 10_000),
    }
    assert [(row["id"], row["difference_minor"]) for row in fund["counts"]] == [
        (matched.json()["id"], 0)
    ]
    # Counts post nothing.
    after = (await api.get(trip.path("/ledger"), headers=owner.headers)).json()
    assert after["ledger_seq"] == seq
    items, _, _ = await pull_all(api, member, f"plan:{trip.plan_id}")
    counted = {item["entity_id"] for item in items if item["entity_type"] == "fund_count"}
    assert counted == {short.json()["id"], matched.json()["id"]}
