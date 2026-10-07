"""The virtual fund, privileged adjustments, and balances that follow merged participants."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
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


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin)


def movement(participant: str, amount: int, currency: str = "USD") -> dict[str, Any]:
    return {
        "participant_id": participant,
        "currency": currency,
        "amount_minor": amount,
        "occurred_on": "2026-10-06",
    }


async def available(api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan) -> dict[str, int]:
    response = await api.get(trip.path("/fund"), headers=user.headers)
    assert response.status_code == 200, response.text
    assert "never holds" in response.json()["notice"]
    return {row["currency"]: row["available_minor"] for row in response.json()["available"]}


async def test_fund_paid_expense_and_its_void_keep_every_currency_balanced(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    for user, person in ((trip.owner, ann), (trip.members["Bea"], bea)):
        given = await api.post(
            trip.path("/fund/contributions"), json=movement(person, 3000), headers=user.headers
        )
        assert given.status_code == 201, given.text
    assert await available(api, trip.owner, trip) == {"USD": 6000}
    dinner = equal_expense(4500, ann, [ann, bea, cam])
    dinner["payers"] = [{"fund": True, "amount_minor": 4500}]
    expense = await add_expense(api, trip.owner, trip, dinner)
    assert expense["revision"]["payers"] == [
        {"participant_id": None, "fund": True, "amount_minor": 4500}
    ]
    assert await available(api, trip.owner, trip) == {"USD": 1500}
    assert await ledger_balances(api, trip.owner, trip) == {
        (None, "USD"): -1500,
        (ann, "USD"): 1500,
        (bea, "USD"): 1500,
        (cam, "USD"): -1500,
    }
    preview = await api.get(trip.path("/ledger/settlement-preview"), headers=trip.owner.headers)
    assert preview.json()[0]["transfers"] == [
        {"from_participant_id": cam, "to_participant_id": min(ann, bea), "amount_minor": 1500}
    ]
    assert preview.json()[0]["fund_payouts"] == [
        {"to_participant_id": max(ann, bea), "amount_minor": 1500}
    ]
    voided = await api.post(
        trip.path(f"/expenses/{expense['id']}/void"), headers=if_match(1, trip.owner)
    )
    assert voided.status_code == 200
    assert await available(api, trip.owner, trip) == {"USD": 6000}
    journal = await api.get(trip.path("/ledger/transactions"), headers=trip.owner.headers)
    kinds = [entry["kind"] for entry in journal.json()["items"]]
    assert kinds == ["fund_contribution", "fund_contribution", "expense", "expense_reversal"]
    for entry in journal.json()["items"]:
        assert sum(p["amount_minor"] for p in entry["postings"]) == 0


async def test_who_moves_fund_money(api: httpx.AsyncClient, trip: FinancePlan) -> None:
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    bea_user = trip.members["Bea"]
    for_someone_else = await api.post(
        trip.path("/fund/contributions"), json=movement(ann, 100), headers=bea_user.headers
    )
    assert for_someone_else.status_code == 403
    for_placeholder = await api.post(
        trip.path("/fund/contributions"), json=movement(cam, 2000), headers=trip.owner.headers
    )
    assert for_placeholder.status_code == 201
    by_member = await api.post(
        trip.path("/fund/withdrawals"), json=movement(bea, 100), headers=bea_user.headers
    )
    assert by_member.status_code == 403
    too_much = await api.post(
        trip.path("/fund/withdrawals"), json=movement(cam, 2001), headers=trip.owner.headers
    )
    assert too_much.status_code == 409 and too_much.json()["code"] == "FUND_INSUFFICIENT"
    back = await api.post(
        trip.path("/fund/withdrawals"), json=movement(cam, 500), headers=trip.owner.headers
    )
    assert back.status_code == 201 and back.json()["kind"] == "withdrawal"
    assert await available(api, trip.owner, trip) == {"USD": 1500}
    wrong_currency = await api.post(
        trip.path("/fund/withdrawals"), json=movement(cam, 1, "EUR"), headers=trip.owner.headers
    )
    assert wrong_currency.status_code == 409
    listed = await api.get(trip.path("/fund/movements"), headers=bea_user.headers)
    assert [row["kind"] for row in listed.json()["items"]] == ["contribution", "withdrawal"]


async def test_fund_settings_name_a_custodian_without_holding_money(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    bea = trip.people["Bea"]
    denied = await api.put(
        trip.path("/fund"),
        json={"custodian_participant_id": bea},
        headers=trip.members["Bea"].headers,
    )
    assert denied.status_code == 403
    created = await api.put(
        trip.path("/fund"),
        json={"custodian_participant_id": bea, "note": "Bea keeps the envelope"},
        headers=trip.owner.headers,
    )
    assert created.status_code == 200 and created.headers["ETag"] == '"1"'
    blind = await api.put(trip.path("/fund"), json={}, headers=trip.owner.headers)
    assert blind.status_code == 428
    stale = await api.put(trip.path("/fund"), json={}, headers=if_match(7, trip.owner))
    assert stale.status_code == 412 and stale.json()["current"]["version"] == 1
    replaced = await api.put(trip.path("/fund"), json={}, headers=if_match(1, trip.owner))
    assert replaced.json()["custodian_participant_id"] is None
    settings = (await api.get(trip.path("/fund"), headers=trip.owner.headers)).json()["settings"]
    assert settings["version"] == 2


async def test_adjustments_are_owner_only_with_a_recent_sign_in(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    body = {
        "currency": "USD",
        "memo": "Fix a cash mix-up",
        "entries": [
            {"participant_id": ann, "amount_minor": 250},
            {"participant_id": bea, "amount_minor": -250},
        ],
    }
    by_member = await api.post(
        trip.path("/ledger/adjustments"), json=body, headers=trip.members["Bea"].headers
    )
    assert by_member.status_code == 403
    unbalanced = await api.post(
        trip.path("/ledger/adjustments"),
        json={**body, "entries": [body["entries"][0], {**body["entries"][1], "amount_minor": -1}]},
        headers=trip.owner.headers,
    )
    assert unbalanced.status_code == 422
    assert unbalanced.json()["code"] == "LEDGER_ENTRY_UNBALANCED"
    drained = await api.post(
        trip.path("/ledger/adjustments"),
        json={
            **body,
            "entries": [
                {"fund": True, "amount_minor": 1},
                {"participant_id": ann, "amount_minor": -1},
            ],
        },
        headers=trip.owner.headers,
    )
    assert drained.status_code == 409 and drained.json()["code"] == "FUND_INSUFFICIENT"
    applied = await api.post(
        trip.path("/ledger/adjustments"), json=body, headers=trip.owner.headers
    )
    assert applied.status_code == 201, applied.text
    assert (applied.json()["kind"], applied.json()["subtype"]) == ("adjustment", "correction")
    assert applied.json()["memo"] == "Fix a cash mix-up"
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): 250, (bea, "USD"): -250}
    audit = admin.fetch(
        "SELECT metadata FROM sync_audit.audit_events WHERE action = 'finance.ledger_adjusted'"
    )
    assert audit == [({"ledger_seq": 1, "subtype": "correction"},)]
    admin.execute(
        "UPDATE iam.sessions SET authenticated_at = now() - interval '1 day' WHERE id = %s",
        trip.owner.session_id,
    )
    stale = await api.post(trip.path("/ledger/adjustments"), json=body, headers=trip.owner.headers)
    assert stale.status_code == 403 and stale.json()["code"] == "STEP_UP_REQUIRED"


async def test_claiming_a_placeholder_moves_its_balances_to_the_survivor(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    bea_user = trip.members["Bea"]
    shared = await add_expense(api, trip.owner, trip, equal_expense(900, ann, [ann, bea, cam]))
    await add_expense(api, trip.owner, trip, equal_expense(400, cam, [ann, cam], currency="EUR"))
    token = (
        await api.post(
            trip.path(f"/participants/{cam}/claim-invites"), json={}, headers=trip.owner.headers
        )
    ).json()["token"]
    merged = await api.post(
        "/v1/invites/redeem",
        json={"token": token, "merge_existing": True},
        headers=bea_user.headers,
    )
    assert merged.status_code == 200, merged.text
    # Cam's -300 USD and +200 EUR now belong to Bea; Cam's accounts are square.
    assert await ledger_balances(api, trip.owner, trip) == {
        (ann, "USD"): 600,
        (bea, "USD"): -600,
        (ann, "EUR"): -200,
        (bea, "EUR"): 200,
    }
    kinds = admin.fetch(
        "SELECT kind, subtype FROM finance.ledger_transactions WHERE plan_id = %s "
        "ORDER BY ledger_seq",
        trip.plan_id,
    )
    assert kinds[-1] == ("adjustment", "merge_transfer")
    keep_cam = await api.put(
        trip.path(f"/expenses/{shared['id']}"),
        json=equal_expense(900, ann, [ann, bea, cam]),
        headers=if_match(1, trip.owner),
    )
    assert keep_cam.json()["code"] == "PARTICIPANT_NOT_ELIGIBLE"
    voided = await api.post(
        trip.path(f"/expenses/{shared['id']}/void"), headers=if_match(1, trip.owner)
    )
    assert voided.status_code == 200
    # The reversal of Cam's old share landed on Bea, so Cam stays at zero.
    assert await ledger_balances(api, trip.owner, trip) == {
        (ann, "EUR"): -200,
        (bea, "EUR"): 200,
    }
    reconciled = admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", trip.plan_id)
    assert reconciled == []


async def test_guest_balances_follow_the_account_they_merge_into(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Ann")
    member = await sign_in(api, identity_provider, subject="bea-sub", name="Bea")
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Trip", "base_currency": "USD"},
            headers=owner.headers,
        )
    ).json()

    async def token() -> str:
        response = await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
        return str(response.json()["token"])

    joined = await api.post(
        "/v1/invites/redeem", json={"token": await token()}, headers=member.headers
    )
    bea = joined.json()["participant"]["id"]
    redeemed = await api.post(
        "/v1/invites/redeem",
        json={"token": await token(), "display_name": "Guest Bea", "device": {"platform": "web"}},
    )
    guest = signed_in_from(redeemed.json()["session"])
    guest_row = redeemed.json()["participant"]["id"]
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    ann = next(p["id"] for p in roster if p["display_name"] == "Ann")
    trip = FinancePlan(plan_id=plan["id"], owner=owner, members={}, people={})
    await add_expense(api, guest, trip, equal_expense(1000, guest_row, [ann, guest_row]))
    merged = await api.post(
        "/v1/auth/google",
        json={
            "id_token": identity_provider.id_token(subject="bea-sub"),
            "merge_guest_participations": True,
        },
        headers=guest.headers,
    )
    assert merged.status_code == 200, merged.text
    assert await ledger_balances(api, owner, trip) == {(ann, "USD"): -500, (bea, "USD"): 500}
    assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", plan["id"]) == []
