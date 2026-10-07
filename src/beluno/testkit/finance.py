"""HTTP helpers for finance tests: a plan with registered members and placeholders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub


@dataclass
class FinancePlan:
    plan_id: str
    owner: SignedIn
    members: dict[str, SignedIn]
    people: dict[str, str]

    def path(self, suffix: str = "") -> str:
        return f"/v1/plans/{self.plan_id}{suffix}"


async def finance_plan(
    api: httpx.AsyncClient,
    provider: IdentityProviderStub,
    admin: AdminDatabase,
    *,
    members: tuple[str, ...] = ("Bea",),
    placeholders: tuple[str, ...] = ("Cam",),
    currency: str = "USD",
    plan_type: str = "trip",
) -> FinancePlan:
    """Owner "Ann", registered members, and name-only placeholders, all active participants."""

    owner = await sign_in(api, provider, name="Ann")
    plan = await api.post(
        "/v1/plans",
        json={
            "type": plan_type,
            "title": "Trip",
            "base_currency": currency,
            "participants": [{"placeholder_name": name} for name in placeholders],
        },
        headers=owner.headers,
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    signed: dict[str, SignedIn] = {}
    for name in members:
        signed[name] = await sign_in(api, provider, name=name)
        await join_with_invite(api, owner, plan_id, signed[name])
    listed = await api.get(f"/v1/plans/{plan_id}/participants", headers=owner.headers)
    assert listed.status_code == 200, listed.text
    people = {row["display_name"]: row["id"] for row in listed.json()}
    return FinancePlan(plan_id=plan_id, owner=owner, members=signed, people=people)


async def join_with_invite(
    api: httpx.AsyncClient, manager: SignedIn, plan_id: str, person: SignedIn
) -> dict[str, Any]:
    """The person joins the plan as a member through a fresh invite link."""

    invite = await api.post(
        f"/v1/plans/{plan_id}/invites", json={"max_uses": 1}, headers=manager.headers
    )
    assert invite.status_code == 201, invite.text
    joined = await api.post(
        "/v1/invites/redeem", json={"token": invite.json()["token"]}, headers=person.headers
    )
    assert joined.status_code == 200, joined.text
    participant: dict[str, Any] = joined.json()["participant"]
    return participant


def equal_expense(
    amount: int,
    payer: str,
    among: list[str],
    *,
    currency: str = "USD",
    description: str = "Dinner",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "description": description,
        "category": "food",
        "occurred_on": "2026-10-06",
        "amount_minor": amount,
        "currency": currency,
        "payers": [{"participant_id": payer, "amount_minor": amount}],
        "split": {"method": "equal", "participant_ids": among},
        **extra,
    }


async def add_expense(
    api: httpx.AsyncClient, user: SignedIn, plan: FinancePlan, body: dict[str, Any]
) -> dict[str, Any]:
    response = await api.post(plan.path("/expenses"), json=body, headers=user.headers)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def ledger_balances(
    api: httpx.AsyncClient, user: SignedIn, plan: FinancePlan
) -> dict[tuple[str | None, str], int]:
    """``(participant_id or None for the fund, currency) -> balance``, zero balances omitted."""

    response = await api.get(plan.path("/ledger"), headers=user.headers)
    assert response.status_code == 200, response.text
    return {
        (row["participant_id"], row["currency"]): row["balance_minor"]
        for row in response.json()["balances"]
        if row["balance_minor"] != 0
    }


def if_match(version: int, user: SignedIn) -> dict[str, str]:
    return {**user.headers, "If-Match": f'"{version}"'}


async def pull_all(
    api: httpx.AsyncClient, user: SignedIn, scope: str, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None, str]:
    """Pull one scope until ``has_more`` is false: items, final cursor, last status."""

    items: list[dict[str, Any]] = []
    while True:
        response = await api.post(
            "/v1/sync/pull",
            json={"scopes": [{"scope": scope, "cursor": cursor}]},
            headers=user.headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()["scopes"][0]
        if page["status"] != "ok":
            return items, page["cursor"], page["status"]
        items.extend(page["changes"])
        cursor = page["cursor"]
        if not page["has_more"]:
            return items, cursor, page["status"]


async def exercise_money_and_members(
    api: httpx.AsyncClient,
    provider: IdentityProviderStub,
    admin: AdminDatabase,
    *,
    memo: str = "Cash mix-up",
) -> FinancePlan:
    """A trip that has used every money and member feature once.

    Owner Ann, members Bea and Dan, placeholder Cam. Expenses (one voided, one
    refunded, one in JPY), a waiver, a reversed settlement, kitty contribution,
    withdrawal and count, a budget, a reversed consolidation, new dates, a base
    currency change to EUR, Dan's capabilities then role changed, an owner
    adjustment between Ann and Dan (with ``memo``), and Bea leaving.
    """

    trip = await finance_plan(api, provider, admin, members=("Bea", "Dan"))
    ann, bea, dan = (trip.people[name] for name in ("Ann", "Bea", "Dan"))
    owner = trip.owner

    async def ok(response: httpx.Response) -> dict[str, Any]:
        assert response.status_code in (200, 201, 204), response.text
        return response.json() if response.status_code != 204 else {}

    voided = await add_expense(api, owner, trip, equal_expense(600, ann, [ann, bea]))
    await ok(
        await api.post(trip.path(f"/expenses/{voided['id']}/void"), headers=if_match(1, owner))
    )
    refunded = await add_expense(api, owner, trip, equal_expense(600, ann, [ann, bea]))
    await ok(
        await api.post(
            trip.path(f"/expenses/{refunded['id']}/refunds"),
            json={"amount_minor": 100, "recipient": {"participant_id": ann}},
            headers=if_match(1, owner),
        )
    )
    settlement = {"currency": "USD", "amount_minor": 50, "occurred_on": "2026-10-07"}
    await ok(
        await api.post(
            trip.path("/waivers"),
            json={"debtor_participant_id": bea, "creditor_participant_id": ann, **settlement},
            headers=owner.headers,
        )
    )
    paid = await ok(
        await api.post(
            trip.path("/settlements"),
            json={"from_participant_id": bea, "to_participant_id": ann, **settlement},
            headers=owner.headers,
        )
    )
    await ok(
        await api.post(trip.path(f"/settlements/{paid['id']}/reverse"), headers=if_match(1, owner))
    )
    kitty = {"participant_id": ann, "currency": "USD", "occurred_on": "2026-10-07"}
    await ok(
        await api.post(
            trip.path("/fund/contributions"),
            json={**kitty, "amount_minor": 500},
            headers=owner.headers,
        )
    )
    await ok(
        await api.post(
            trip.path("/fund/withdrawals"),
            json={**kitty, "amount_minor": 200},
            headers=owner.headers,
        )
    )
    await ok(
        await api.post(
            trip.path("/fund/counts"),
            json={"currency": "USD", "counted_minor": 300},
            headers=owner.headers,
        )
    )
    await ok(
        await api.post(
            trip.path("/budgets"),
            json={"scope": "total", "limit_minor": 9_000},
            headers=owner.headers,
        )
    )
    await add_expense(api, owner, trip, equal_expense(3_000, ann, [ann, bea], currency="JPY"))
    consolidation = await ok(
        await api.post(
            trip.path("/ledger/consolidations"),
            json={"base_currency": "USD", "rates": [{"currency": "JPY", "rate": "0.0067"}]},
            headers=owner.headers,
        )
    )
    await ok(
        await api.post(
            trip.path(f"/ledger/consolidations/{consolidation['id']}/reverse"),
            headers=if_match(1, owner),
        )
    )
    version = (await api.get(trip.path(), headers=owner.headers)).json()["version"]
    retimed = await ok(
        await api.patch(
            trip.path(),
            json={"timing": {"mode": "date", "start_date": "2027-03-20", "end_date": "2027-03-27"}},
            headers=if_match(version, owner),
        )
    )
    await ok(
        await api.post(
            trip.path("/base-currency"),
            json={"currency": "EUR", "rate": {"rate": "0.9"}},
            headers=if_match(retimed["version"], owner),
        )
    )
    await ok(
        await api.patch(
            trip.path(f"/participants/{dan}"),
            json={"capabilities": ["expenses.manage"]},
            headers=if_match(1, owner),
        )
    )
    await ok(
        await api.patch(
            trip.path(f"/participants/{dan}"), json={"role": "viewer"}, headers=if_match(2, owner)
        )
    )
    await ok(
        await api.post(
            trip.path("/ledger/adjustments"),
            json={
                "currency": "EUR",
                "memo": memo,
                "entries": [
                    {"participant_id": ann, "amount_minor": 250},
                    {"participant_id": dan, "amount_minor": -250},
                ],
            },
            headers=owner.headers,
        )
    )
    await ok(await api.post(trip.path("/leave"), headers=trip.members["Bea"].headers))

    return trip
