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
) -> FinancePlan:
    """Owner "Ann", registered members, and name-only placeholders, all active participants."""

    owner = await sign_in(api, provider, name="Ann")
    plan = await api.post(
        "/v1/plans",
        json={
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
