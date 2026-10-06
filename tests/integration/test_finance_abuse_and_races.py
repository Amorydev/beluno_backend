"""Finance under abuse and races: merges racing writes, forged debts, waivers, fund spending."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

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


def payment(debtor: str, creditor: str, amount: int, currency: str = "USD") -> dict[str, Any]:
    return {
        "from_participant_id": debtor,
        "to_participant_id": creditor,
        "currency": currency,
        "amount_minor": amount,
        "occurred_on": "2026-10-07",
    }


def waiver(debtor: str, creditor: str, amount: int) -> dict[str, Any]:
    return {
        "debtor_participant_id": debtor,
        "creditor_participant_id": creditor,
        "currency": "USD",
        "amount_minor": amount,
        "occurred_on": "2026-10-07",
    }


async def claim_token(api: httpx.AsyncClient, trip: FinancePlan, placeholder: str) -> str:
    response = await api.post(
        trip.path(f"/participants/{placeholder}/claim-invites"), json={}, headers=trip.owner.headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


async def promote(api: httpx.AsyncClient, trip: FinancePlan, name: str, role: str) -> None:
    response = await api.patch(
        trip.path(f"/participants/{trip.people[name]}"),
        json={"role": role},
        headers=if_match(1, trip.owner),
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("warm", [False, True])
async def test_a_merge_racing_finance_writes_never_strands_money(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    warm: bool,
) -> None:
    for _ in range(4):
        trip = await finance_plan(api, identity_provider, admin)
        ann, cam = trip.people["Ann"], trip.people["Cam"]
        if warm:
            await add_expense(api, trip.owner, trip, equal_expense(100, ann, [ann, cam]))
        token = await claim_token(api, trip, cam)
        expense, merge = await asyncio.gather(
            api.post(
                trip.path("/expenses"),
                json=equal_expense(300, ann, [ann, cam]),
                headers=trip.owner.headers,
            ),
            api.post(
                "/v1/invites/redeem",
                json={"token": token, "merge_existing": True},
                headers=trip.members["Bea"].headers,
            ),
        )
        assert merge.status_code == 200, merge.text
        # Either the expense ran first (and its debt moved to Bea) or it saw Cam merged.
        assert expense.status_code in (201, 422), expense.text
        assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", trip.plan_id) == []
        balances = await ledger_balances(api, trip.owner, trip)
        assert all(participant != cam for participant, _ in balances)


async def test_waivers_only_forgive_real_debt_and_never_your_own(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    bea, cam = trip.people["Bea"], trip.people["Cam"]
    await promote(api, trip, "Bea", "admin")
    await add_expense(api, trip.owner, trip, equal_expense(1000, cam, [bea, cam]))
    bea_user = trip.members["Bea"]
    # Bea (an admin) owes placeholder Cam 500: she cannot forgive her own debt for Cam.
    own = await api.post(
        trip.path("/waivers"), json=waiver(bea, cam, 500), headers=bea_user.headers
    )
    assert own.status_code == 403
    too_much = await api.post(
        trip.path("/waivers"), json=waiver(bea, cam, 10**12), headers=trip.owner.headers
    )
    assert too_much.status_code == 409 and too_much.json()["code"] == "WAIVER_EXCEEDS_DEBT"
    forgiven = await api.post(
        trip.path("/waivers"), json=waiver(bea, cam, 500), headers=trip.owner.headers
    )
    assert forgiven.status_code == 201
    assert await ledger_balances(api, trip.owner, trip) == {}
    # Bea cannot record "I paid Cam" and then confirm it on Cam's behalf.
    paid = await api.post(
        trip.path("/settlements"), json=payment(bea, cam, 5), headers=bea_user.headers
    )
    confirm = await api.post(
        trip.path(f"/settlements/{paid.json()['id']}/confirm"), headers=bea_user.headers
    )
    assert confirm.status_code == 403
    by_owner = await api.post(
        trip.path(f"/settlements/{paid.json()['id']}/confirm"), headers=trip.owner.headers
    )
    # The owner answers as a manager for the placeholder creditor.
    assert by_owner.status_code == 200


async def test_a_creditor_can_undo_a_payment_they_never_received(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    bea, dan = trip.people["Bea"], trip.people["Dan"]
    bea_user, dan_user = trip.members["Bea"], trip.members["Dan"]
    forged = await api.post(
        trip.path("/settlements"), json=payment(bea, dan, 10**12), headers=bea_user.headers
    )
    path = trip.path(f"/settlements/{forged.json()['id']}")
    disputed = await api.post(path + "/dispute", headers=dan_user.headers)
    assert disputed.json()["status"] == "disputed"
    undone = await api.post(path + "/reverse", headers=if_match(2, dan_user))
    assert undone.status_code == 200, undone.text
    assert await ledger_balances(api, trip.owner, trip) == {}
    status = (await api.get(trip.path("/ledger"), headers=trip.owner.headers)).json()
    assert (status["status"], status["disputed_settlements"]) == ("open", 0)
    # Once the creditor confirmed a payment, only its recorder or a manager reverses it.
    real = await api.post(
        trip.path("/settlements"), json=payment(bea, dan, 100), headers=bea_user.headers
    )
    real_path = trip.path(f"/settlements/{real.json()['id']}")
    await api.post(real_path + "/confirm", headers=dan_user.headers)
    refused = await api.post(real_path + "/reverse", headers=if_match(2, dan_user))
    assert refused.status_code == 403


async def test_every_creditor_can_be_answered_for(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    ann, bea, cam, dan = (trip.people[name] for name in ("Ann", "Bea", "Cam", "Dan"))
    await promote(api, trip, "Dan", "viewer")
    to_viewer = await api.post(
        trip.path("/settlements"), json=payment(ann, dan, 10), headers=trip.owner.headers
    )
    confirmed = await api.post(
        trip.path(f"/settlements/{to_viewer.json()['id']}/confirm"),
        headers=trip.members["Dan"].headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    to_cam = await api.post(
        trip.path("/settlements"), json=payment(ann, cam, 20), headers=trip.owner.headers
    )
    merged = await api.post(
        "/v1/invites/redeem",
        json={"token": await claim_token(api, trip, cam), "merge_existing": True},
        headers=trip.members["Bea"].headers,
    )
    assert merged.status_code == 200
    # Cam's money now belongs to Bea, so Bea answers for it.
    answered = await api.post(
        trip.path(f"/settlements/{to_cam.json()['id']}/dispute"),
        headers=trip.members["Bea"].headers,
    )
    assert answered.status_code == 200, answered.text
    removed = await api.delete(trip.path(f"/participants/{dan}"), headers=trip.owner.headers)
    assert removed.status_code in (200, 204)
    to_removed = await api.post(
        trip.path("/settlements"), json=payment(bea, dan, 5), headers=trip.members["Bea"].headers
    )
    by_manager = await api.post(
        trip.path(f"/settlements/{to_removed.json()['id']}/confirm"), headers=trip.owner.headers
    )
    assert by_manager.status_code == 200


async def test_only_the_custodian_or_a_manager_spends_the_fund(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    ann, bea, dan = (trip.people[name] for name in ("Ann", "Bea", "Dan"))
    await api.post(
        trip.path("/fund/contributions"),
        json={
            "participant_id": ann,
            "currency": "USD",
            "amount_minor": 1000,
            "occurred_on": "2026-10-06",
        },
        headers=trip.owner.headers,
    )
    spend = equal_expense(400, ann, [dan])
    spend["payers"] = [{"fund": True, "amount_minor": 400}]
    member = await api.post(trip.path("/expenses"), json=spend, headers=trip.members["Dan"].headers)
    assert member.status_code == 403
    await api.put(
        trip.path("/fund"), json={"custodian_participant_id": bea}, headers=trip.owner.headers
    )
    custodian = await api.post(
        trip.path("/expenses"), json=spend, headers=trip.members["Bea"].headers
    )
    assert custodian.status_code == 201, custodian.text
    # Money refunded into the fund and then withdrawn cannot be voided back out of it.
    dinner = await add_expense(api, trip.owner, trip, equal_expense(400, ann, [ann, dan]))
    refund = await api.post(
        trip.path(f"/expenses/{dinner['id']}/refunds"),
        json={"amount_minor": 400, "recipient": {"fund": True}},
        headers=if_match(1, trip.owner),
    )
    assert refund.status_code == 200, refund.text
    drained = await api.post(
        trip.path("/fund/withdrawals"),
        json={
            "participant_id": ann,
            "currency": "USD",
            "amount_minor": 1000,
            "occurred_on": "2026-10-06",
        },
        headers=trip.owner.headers,
    )
    assert drained.status_code == 201, drained.text
    void = await api.post(
        trip.path(f"/expenses/{dinner['id']}/void"), headers=if_match(2, trip.owner)
    )
    assert void.status_code == 409 and void.json()["code"] == "FUND_INSUFFICIENT"
    # Voiding the fund's own spend gives the money back and nets to zero.
    undone = await api.post(
        trip.path(f"/expenses/{custodian.json()['id']}/void"),
        headers=if_match(1, trip.members["Bea"]),
    )
    assert undone.status_code == 200, undone.text


async def test_control_characters_are_refused_before_they_reach_storage(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann = trip.people["Ann"]
    body = equal_expense(100, ann, [ann], description="Secret\x00dinner")
    response = await api.post(trip.path("/expenses"), json=body, headers=trip.owner.headers)
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    noted = equal_expense(100, ann, [ann], notes="line one\nline\ttwo")
    assert (
        await api.post(trip.path("/expenses"), json=noted, headers=trip.owner.headers)
    ).status_code == 201


async def test_a_linked_expense_revised_with_its_commitment_keeps_it(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann = trip.people["Ann"]
    commitment = await api.post(
        trip.path("/commitments"),
        json={"description": "Cabin", "currency": "USD", "amount_minor": 500},
        headers=trip.owner.headers,
    )
    cid = commitment.json()["id"]
    expense = await add_expense(
        api, trip.owner, trip, equal_expense(500, ann, [ann], commitment_id=cid)
    )
    kept = await api.put(
        trip.path(f"/expenses/{expense['id']}"),
        json=equal_expense(550, ann, [ann], commitment_id=cid),
        headers=if_match(1, trip.owner),
    )
    assert kept.status_code == 200
    listed = (await api.get(trip.path("/commitments"), headers=trip.owner.headers)).json()
    assert (listed[0]["state"], listed[0]["expense_id"]) == ("converted_to_expense", expense["id"])
    # A converted commitment stays editable without leaving its state.
    edited = await api.put(
        trip.path(f"/commitments/{cid}"),
        json={
            "description": "Cabin (2 nights)",
            "currency": "USD",
            "amount_minor": 500,
            "state": "converted_to_expense",
        },
        headers=if_match(listed[0]["version"], trip.owner),
    )
    assert edited.status_code == 200, edited.text


async def test_merged_money_and_repairs_are_visible_to_operators_and_devices(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, cam = trip.people["Ann"], trip.people["Cam"]
    await add_expense(api, trip.owner, trip, equal_expense(200, ann, [ann, cam]))
    # An out-of-band merge (no transfer) is exactly what reconciliation must catch.
    admin.execute(
        "UPDATE plans.plan_participants SET access_state = 'merged', "
        "merged_into_participant_id = %s WHERE id = %s",
        ann,
        cam,
    )
    problems = [
        row[0] for row in admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", trip.plan_id)
    ]
    assert problems == ["merged_balance"]
