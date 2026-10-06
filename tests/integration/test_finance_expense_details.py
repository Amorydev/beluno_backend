"""Expense time, the adjustment split, personal spend, and readable revision history."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.db.ids import new_id
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


async def test_expenses_carry_their_local_time_and_zone(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    # 20:10 in Tokyo on the 21st is still the 21st there, though it is the 21st 11:10 UTC.
    timed = equal_expense(
        1_000,
        ann,
        [ann, bea],
        occurred_on="2027-03-21",
        occurred_at="2027-03-21T11:10:00Z",
        occurred_timezone="Asia/Tokyo",
    )
    created = await add_expense(api, trip.owner, trip, timed)
    revision = created["revision"]
    assert revision["occurred_at"] == "2027-03-21T11:10:00Z"
    assert revision["occurred_timezone"] == "Asia/Tokyo"

    for overrides in (
        {"occurred_at": "2027-03-21T11:10:00Z"},
        {"occurred_timezone": "Asia/Tokyo"},
        {"occurred_at": "2027-03-21T16:00:00Z", "occurred_timezone": "Asia/Tokyo"},
        {"occurred_at": "2027-03-21T11:10:00Z", "occurred_timezone": "Mars/Base"},
    ):
        body = {**equal_expense(1_000, ann, [ann, bea], occurred_on="2027-03-21"), **overrides}
        refused = await api.post(trip.path("/expenses"), json=body, headers=trip.owner.headers)
        assert refused.status_code == 422, overrides


async def test_the_adjustment_split_and_personal_spend(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    adjusted: dict[str, Any] = {
        **equal_expense(1_000, ann, [ann]),
        "split": {
            "method": "adjustment",
            "shares": [
                {"participant_id": ann, "adjustment_minor": 100},
                {"participant_id": bea, "adjustment_minor": 0},
                {"participant_id": cam, "adjustment_minor": -100},
            ],
        },
    }
    created = await add_expense(api, trip.owner, trip, adjusted)
    shares = {row["participant_id"]: row["owed_minor"] for row in created["revision"]["shares"]}
    assert shares == {ann: 434, bea: 333, cam: 233}
    assert created["revision"]["split"] == adjusted["split"]
    assert created["revision"]["personal"] is False
    negative = {
        **adjusted,
        "split": {
            "method": "adjustment",
            "shares": [
                {"participant_id": ann, "adjustment_minor": 0},
                {"participant_id": bea, "adjustment_minor": 2_000},
            ],
        },
    }
    refused = await api.post(trip.path("/expenses"), json=negative, headers=trip.owner.headers)
    assert refused.status_code == 422 and refused.json()["code"] == "SPLIT_INVALID"

    # Paid by Bea for Bea alone: personal, and it moves no balance.
    before = await ledger_balances(api, trip.owner, trip)
    personal = await add_expense(api, trip.owner, trip, equal_expense(700, bea, [bea]))
    assert personal["revision"]["personal"] is True
    assert await ledger_balances(api, trip.owner, trip) == before


async def test_revisions_say_where_they_came_from(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    online = await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    assert (online["revision"]["source"], online["revision"]["client_created_at"]) == ("http", None)
    assert online["revision"]["device_label"] == "Test device"

    pushed = await api.post(
        "/v1/sync/push",
        json={
            "device_id": "phone-1",
            "operations": [
                {
                    "operation_id": str(new_id()),
                    "command": "expense.revise",
                    "schema_version": 1,
                    "target": {"plan_id": trip.plan_id, "expense_id": online["id"]},
                    "expected_version": 1,
                    "depends_on": [],
                    "payload": equal_expense(900, ann, [ann, bea]),
                    "client_created_at": "2026-10-05T23:40:00Z",
                }
            ],
        },
        headers=trip.owner.headers,
    )
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["results"][0]["outcome"] == "applied"
    revisions = await api.get(
        trip.path(f"/expenses/{online['id']}/revisions"), headers=trip.owner.headers
    )
    history = [
        (row["revision_number"], row["source"], row["client_created_at"], row["amount_minor"])
        for row in revisions.json()
    ]
    assert history == [
        (1, "http", None, 600),
        (2, "sync", "2026-10-05T23:40:00Z", 900),
    ]
    edited = await api.put(
        trip.path(f"/expenses/{online['id']}"),
        json=equal_expense(800, ann, [ann, bea]),
        headers=if_match(2, trip.owner),
    )
    assert edited.status_code == 200 and edited.json()["revision"]["source"] == "http"
