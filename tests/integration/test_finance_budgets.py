"""Budgets, cost commitments, the finance port, and the no-double-count rule."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from beluno.auth import AuthenticatedActor
from beluno.authorization.access import load_plan
from beluno.db.ids import new_id
from beluno.modules.context import Runtime, open_context
from beluno.modules.finance.commitments import COST_COMMITMENTS, CommitmentDraft
from beluno.modules.finance.states import CommitmentState
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


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin)


async def overview(api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan) -> dict[str, Any]:
    response = await api.get(trip.path("/budgets"), headers=user.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def totals(body: dict[str, Any]) -> tuple[int, int, int, int]:
    spend = body["total"]
    return (
        spend["actual_minor"],
        spend["committed_minor"],
        spend["estimated_minor"],
        spend["projected_minor"],
    )


async def test_budget_crud_permissions_and_tombstones(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    created = await api.post(
        trip.path("/budgets"),
        json={"scope": "total", "limit_minor": 10_000},
        headers=trip.owner.headers,
    )
    assert created.status_code == 201, created.text
    budget = created.json()
    assert (budget["currency"], budget["version"]) == ("USD", 1)
    by_member = await api.post(
        trip.path("/budgets"),
        json={"scope": "category", "category": "food", "limit_minor": 1},
        headers=trip.members["Bea"].headers,
    )
    assert by_member.status_code == 403
    duplicate = await api.post(
        trip.path("/budgets"), json={"scope": "total", "limit_minor": 1}, headers=trip.owner.headers
    )
    assert duplicate.status_code == 409
    mismatched = await api.post(
        trip.path("/budgets"),
        json={"scope": "category", "limit_minor": 1},
        headers=trip.owner.headers,
    )
    assert mismatched.status_code == 422
    path = trip.path(f"/budgets/{budget['id']}")
    updated = await api.patch(path, json={"limit_minor": 12_000}, headers=if_match(1, trip.owner))
    assert updated.json()["limit_minor"] == 12_000 and updated.json()["version"] == 2
    stale = await api.patch(path, json={"limit_minor": 1}, headers=if_match(1, trip.owner))
    assert stale.status_code == 412 and stale.json()["current"]["limit_minor"] == 12_000

    items, cursor, _ = await pull_all(api, trip.members["Bea"], f"plan:{trip.plan_id}")
    assert ("budget", budget["id"]) in {(i["entity_type"], i["entity_id"]) for i in items}
    deleted = await api.delete(path, headers=trip.owner.headers)
    assert deleted.status_code == 204
    changes, _, _ = await pull_all(api, trip.members["Bea"], f"plan:{trip.plan_id}", cursor)
    tombstone = next(c for c in changes if c["entity_id"] == budget["id"])
    assert (tombstone["operation"], tombstone["version"]) == ("delete", 3)
    # A deleted scope can be budgeted again.
    again = await api.post(
        trip.path("/budgets"), json={"scope": "total", "limit_minor": 5}, headers=trip.owner.headers
    )
    assert again.status_code == 201


async def test_overview_converts_labels_and_never_adds_unconverted_spend(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    for body in (
        {"scope": "total", "limit_minor": 10_000},
        {"scope": "category", "category": "food", "limit_minor": 5_000},
        {"scope": "participant", "participant_id": bea, "limit_minor": 1_000},
        {"scope": "daily", "limit_minor": 4_000},
    ):
        response = await api.post(trip.path("/budgets"), json=body, headers=trip.owner.headers)
        assert response.status_code == 201, response.text
    await add_expense(api, trip.owner, trip, equal_expense(6000, ann, [ann, bea, cam]))
    transport = equal_expense(1000, bea, [bea], currency="JPY", description="Train")
    transport.update(
        category="transport",
        occurred_on="2026-10-07",
        base_rate={"rate": "0.0067", "source": "estimated"},
    )
    await add_expense(api, trip.owner, trip, transport)
    await add_expense(api, trip.owner, trip, equal_expense(2500, ann, [ann], currency="KWD"))
    body = await overview(api, trip.owner, trip)
    assert body["currency"] == "USD"
    assert totals(body) == (6670, 0, 0, 6670)
    assert body["estimated_rates"] is True
    assert body["unconverted"] == [{"tier": "actual", "currency": "KWD", "amount_minor": 2500}]
    categories = {row["category"]: row["spend"]["actual_minor"] for row in body["categories"]}
    assert categories == {"food": 6000, "transport": 670}
    usage = {row["budget"]["scope"]: row for row in body["budgets"]}
    assert usage["total"]["remaining_minor"] == 10_000 - 6670
    assert usage["category"]["over_limit"] is True
    assert usage["participant"]["spend"]["actual_minor"] == 2000 + 670
    assert usage["participant"]["over_limit"] is True
    assert usage["daily"]["days"] == [
        {"date": "2026-10-06", "actual_minor": 6000},
        {"date": "2026-10-07", "actual_minor": 670},
    ]
    assert usage["daily"]["over_limit"] is True


async def test_a_commitment_and_its_expense_count_once(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    created = await api.post(
        trip.path("/commitments"),
        json={
            "category": "lodging",
            "description": "Cabin",
            "currency": "USD",
            "amount_minor": 5000,
        },
        headers=trip.owner.headers,
    )
    assert created.status_code == 201, created.text
    commitment = created.json()
    assert (commitment["state"], commitment["source_type"]) == ("estimated", "manual")
    assert totals(await overview(api, trip.owner, trip)) == (0, 0, 5000, 5000)
    path = trip.path(f"/commitments/{commitment['id']}")
    committed = await api.put(
        path,
        json={
            "category": "lodging",
            "description": "Cabin",
            "currency": "USD",
            "amount_minor": 5000,
            "state": "committed",
        },
        headers=if_match(1, trip.owner),
    )
    assert committed.status_code == 200, committed.text
    assert totals(await overview(api, trip.owner, trip)) == (0, 5000, 0, 5000)

    paid = equal_expense(5200, ann, [ann, bea], commitment_id=commitment["id"])
    expense = await add_expense(api, trip.owner, trip, paid)
    assert expense["revision"]["commitment_id"] == commitment["id"]
    listed = (await api.get(trip.path("/commitments"), headers=trip.owner.headers)).json()
    assert (listed[0]["state"], listed[0]["expense_id"]) == ("converted_to_expense", expense["id"])
    assert totals(await overview(api, trip.owner, trip)) == (5200, 0, 0, 5200)

    twice = await api.post(
        trip.path("/expenses"),
        json=equal_expense(10, ann, [ann], commitment_id=commitment["id"]),
        headers=trip.owner.headers,
    )
    assert twice.status_code == 409
    unknown = await api.post(
        trip.path("/expenses"),
        json=equal_expense(10, ann, [ann], commitment_id=str(new_id())),
        headers=trip.owner.headers,
    )
    assert unknown.status_code == 422

    voided = await api.post(
        trip.path(f"/expenses/{expense['id']}/void"), headers=if_match(1, trip.owner)
    )
    assert voided.status_code == 200
    restored = (await api.get(trip.path("/commitments"), headers=trip.owner.headers)).json()
    assert (restored[0]["state"], restored[0]["expense_id"]) == ("committed", None)
    assert totals(await overview(api, trip.owner, trip)) == (0, 5000, 0, 5000)


async def test_revising_an_expense_moves_its_commitment(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann = trip.people["Ann"]
    first, second = [
        (
            await api.post(
                trip.path("/commitments"),
                json={
                    "description": name,
                    "currency": "USD",
                    "amount_minor": 1000,
                    "state": "committed",
                },
                headers=trip.owner.headers,
            )
        ).json()
        for name in ("Tickets", "Museum")
    ]
    expense = await add_expense(
        api, trip.owner, trip, equal_expense(900, ann, [ann], commitment_id=first["id"])
    )
    moved = await api.put(
        trip.path(f"/expenses/{expense['id']}"),
        json=equal_expense(1100, ann, [ann], commitment_id=second["id"]),
        headers=if_match(1, trip.owner),
    )
    assert moved.status_code == 200, moved.text
    states = {
        c["description"]: c["state"]
        for c in (await api.get(trip.path("/commitments"), headers=trip.owner.headers)).json()
    }
    assert states == {"Tickets": "committed", "Museum": "converted_to_expense"}
    assert totals(await overview(api, trip.owner, trip)) == (1100, 1000, 0, 2100)


async def test_other_modules_record_costs_through_the_port(
    runtime: Runtime, api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    owner = trip.owner
    actor = AuthenticatedActor(
        user_id=UUID(owner.user_id),
        session_id=UUID(owner.session_id),
        authenticated_at=datetime.now(UTC),
        is_guest=False,
    )
    booking_id = new_id()
    draft = CommitmentDraft(
        category="lodging",
        description="Hotel booking",
        currency="EUR",
        amount_minor=20_000,
        state=CommitmentState.COMMITTED,
    )
    async with open_context(runtime, actor) as ctx:
        access = await load_plan(ctx, UUID(trip.plan_id), for_update=True)
        recorded = await COST_COMMITMENTS.record(
            ctx,
            access,
            source_type="booking",
            source_id=booking_id,
            commitment_kind="deposit",
            draft=draft,
        )
        commitment_id = recorded.id
    async with open_context(runtime, actor) as ctx:
        access = await load_plan(ctx, UUID(trip.plan_id), for_update=True)
        again = await COST_COMMITMENTS.record(
            ctx,
            access,
            source_type="booking",
            source_id=booking_id,
            commitment_kind="deposit",
            draft=CommitmentDraft(**{**draft.__dict__, "amount_minor": 21_000}),
        )
        assert again.id == commitment_id and again.version == 2
    body = await overview(api, owner, trip)
    assert body["unconverted"] == [{"tier": "committed", "currency": "EUR", "amount_minor": 21_000}]
    edited = await api.put(
        trip.path(f"/commitments/{commitment_id}"),
        json={
            "description": "x",
            "currency": "EUR",
            "amount_minor": 1,
            "state": "cancelled",
        },
        headers=if_match(2, owner),
    )
    assert edited.status_code == 403
    async with open_context(runtime, actor) as ctx:
        access = await load_plan(ctx, UUID(trip.plan_id), for_update=True)
        await COST_COMMITMENTS.cancel(
            ctx, access, source_type="booking", source_id=booking_id, commitment_kind="deposit"
        )
    listed = (await api.get(trip.path("/commitments"), headers=owner.headers)).json()
    assert [(c["source_type"], c["state"]) for c in listed] == [("booking", "cancelled")]
    assert (await overview(api, owner, trip))["unconverted"] == []


async def test_the_first_budget_publishes_the_ledger_to_synced_devices(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    scope = f"plan:{trip.plan_id}"
    _, cursor, _ = await pull_all(api, trip.owner, scope)
    created = await api.post(
        trip.path("/budgets"),
        json={"scope": "total", "limit_minor": 90_000},
        headers=trip.owner.headers,
    )
    assert created.status_code == 201, created.text

    changes, _, status = await pull_all(api, trip.owner, scope, cursor)
    assert status == "ok"
    ledger = [item for item in changes if item["entity_type"] == "ledger"]
    assert [(item["entity_id"], item["operation"]) for item in ledger] == [
        (trip.plan_id, "upsert")
    ]
    assert ledger[0]["data"]["ledger_seq"] == 0
