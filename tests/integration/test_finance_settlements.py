"""Settlements, waivers, cross-currency payments, ledger status, and ledger views."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.db.ids import new_id
from beluno.testkit.api_client import SignedIn
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    ledger_balances,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin)


def payment(
    debtor: str, creditor: str, amount: int, currency: str = "USD", **extra: Any
) -> dict[str, Any]:
    return {
        "from_participant_id": debtor,
        "to_participant_id": creditor,
        "currency": currency,
        "amount_minor": amount,
        "occurred_on": "2026-10-07",
        **extra,
    }


async def settle(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, body: dict[str, Any]
) -> httpx.Response:
    return await api.post(trip.path("/settlements"), json=body, headers=user.headers)


async def ledger(api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan) -> dict[str, Any]:
    response = await api.get(trip.path("/ledger"), headers=user.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_settlement_lifecycle_moves_status_and_balances(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    bea_user = trip.members["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    recorded = await settle(api, bea_user, trip, payment(bea, ann, 300, method="cash"))
    assert recorded.status_code == 201, recorded.text
    settlement = recorded.json()
    assert (settlement["status"], settlement["overpaid"], settlement["version"]) == (
        "recorded",
        False,
        1,
    )
    assert await ledger_balances(api, trip.owner, trip) == {}
    assert (await ledger(api, trip.owner, trip))["status"] == "settled"

    path = trip.path(f"/settlements/{settlement['id']}")
    by_debtor = await api.post(path + "/confirm", headers=bea_user.headers)
    assert by_debtor.status_code == 403
    disputed = await api.post(path + "/dispute", headers=trip.owner.headers)
    assert disputed.json()["status"] == "disputed"
    assert (await ledger(api, trip.owner, trip))["disputed_settlements"] == 1
    confirmed = await api.post(path + "/confirm", headers=trip.owner.headers)
    assert confirmed.json()["status"] == "confirmed" and confirmed.json()["version"] == 3
    assert (await ledger(api, trip.owner, trip))["disputed_settlements"] == 0
    # Answering again is a no-op, not an error.
    repeat = await api.post(path + "/confirm", headers=trip.owner.headers)
    assert repeat.status_code == 200 and repeat.json()["version"] == 3

    stale = await api.post(path + "/reverse", headers=if_match(1, bea_user))
    assert stale.status_code == 412 and stale.json()["current"]["status"] == "confirmed"
    reversed_ = await api.post(path + "/reverse", headers=if_match(3, bea_user))
    assert reversed_.status_code == 200, reversed_.text
    assert reversed_.json()["status"] == "reversed"
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): 300, (bea, "USD"): -300}
    assert (await ledger(api, trip.owner, trip))["status"] == "reopened"
    again = await api.post(path + "/reverse", headers=if_match(4, bea_user))
    assert again.status_code == 409


async def test_creditor_records_confirmed_and_overpayment_is_flagged(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    response = await settle(api, trip.owner, trip, payment(bea, ann, 500, fee_minor=25))
    body = response.json()
    assert (body["status"], body["overpaid"], body["fee_minor"]) == ("confirmed", True, 25)
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): -200, (bea, "USD"): 200}


async def test_who_may_record_and_answer(api: httpx.AsyncClient, trip: FinancePlan) -> None:
    ann, bea, cam = trip.people["Ann"], trip.people["Bea"], trip.people["Cam"]
    bea_user = trip.members["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(900, cam, [ann, bea, cam]))
    outsider_pair = await settle(api, bea_user, trip, payment(ann, cam, 300))
    assert outsider_pair.status_code == 403
    managed = await settle(api, trip.owner, trip, payment(bea, cam, 300))
    assert managed.status_code == 201
    # Cam is a placeholder creditor: a manager answers for them, a member cannot.
    path = trip.path(f"/settlements/{managed.json()['id']}")
    assert (await api.post(path + "/confirm", headers=bea_user.headers)).status_code == 403
    assert (await api.post(path + "/confirm", headers=trip.owner.headers)).status_code == 200
    # The recorder or a manager reverses; another member may not.
    own = await settle(api, bea_user, trip, payment(bea, ann, 1))
    other = await api.post(
        trip.path(f"/settlements/{own.json()['id']}/reverse"), headers=if_match(1, trip.owner)
    )
    assert other.status_code == 200


async def test_waivers_are_the_creditors_to_give(api: httpx.AsyncClient, trip: FinancePlan) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    bea_user = trip.members["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    waiver = {
        "debtor_participant_id": bea,
        "creditor_participant_id": ann,
        "currency": "USD",
        "amount_minor": 100,
        "occurred_on": "2026-10-07",
    }
    denied = await api.post(trip.path("/waivers"), json=waiver, headers=bea_user.headers)
    assert denied.status_code == 403
    given = await api.post(trip.path("/waivers"), json=waiver, headers=trip.owner.headers)
    assert given.status_code == 201, given.text
    assert (given.json()["kind"], given.json()["status"]) == ("waiver", "confirmed")
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): 200, (bea, "USD"): -200}
    answer = await api.post(
        trip.path(f"/settlements/{given.json()['id']}/dispute"), headers=trip.owner.headers
    )
    assert answer.status_code == 409
    undone = await api.post(
        trip.path(f"/settlements/{given.json()['id']}/reverse"), headers=if_match(1, trip.owner)
    )
    assert undone.status_code == 200
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): 300, (bea, "USD"): -300}


async def test_paying_in_another_currency_converts_explicitly(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(2000, ann, [ann, bea], currency="EUR"))
    paid = await settle(
        api,
        trip.members["Bea"],
        trip,
        payment(bea, ann, 1000, currency="EUR", paid={"currency": "JPY", "amount_minor": 1650}),
    )
    assert paid.status_code == 201, paid.text
    assert paid.json()["paid"] == {"currency": "JPY", "amount_minor": 1650, "rate": "165"}
    assert await ledger_balances(api, trip.owner, trip) == {}
    kinds = admin.fetch(
        "SELECT kind FROM finance.ledger_transactions WHERE settlement_id = %s ORDER BY ledger_seq",
        paid.json()["id"],
    )
    assert kinds == [("settlement",), ("conversion",)]
    journal = await api.get(trip.path("/ledger/transactions"), headers=trip.owner.headers)
    for entry in journal.json()["items"]:
        for currency in {p["currency"] for p in entry["postings"]}:
            assert (
                sum(p["amount_minor"] for p in entry["postings"] if p["currency"] == currency) == 0
            )
    reversed_ = await api.post(
        trip.path(f"/settlements/{paid.json()['id']}/reverse"),
        headers=if_match(1, trip.members["Bea"]),
    )
    assert reversed_.status_code == 200
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "EUR"): 1000, (bea, "EUR"): -1000}
    same = await settle(
        api,
        trip.owner,
        trip,
        payment(bea, ann, 10, currency="EUR", paid={"currency": "EUR", "amount_minor": 10}),
    )
    assert same.status_code == 422


async def test_settlements_continue_after_the_plan_is_completed(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    for version, state in ((1, "active"), (2, "completed")):
        moved = await api.post(
            trip.path("/state"), json={"state": state}, headers=if_match(version, trip.owner)
        )
        assert moved.status_code == 200, moved.text
    expense = await api.post(
        trip.path("/expenses"), json=equal_expense(10, ann, [ann]), headers=trip.owner.headers
    )
    assert expense.status_code == 403
    paid = await settle(api, trip.members["Bea"], trip, payment(bea, ann, 300))
    assert paid.status_code == 201
    assert (await ledger(api, trip.owner, trip))["status"] == "settled"


async def test_preview_explanation_and_journal(api: httpx.AsyncClient, trip: FinancePlan) -> None:
    ann, bea, cam = trip.people["Ann"], trip.people["Bea"], trip.people["Cam"]
    first = await add_expense(api, trip.owner, trip, equal_expense(900, ann, [ann, bea, cam]))
    await add_expense(
        api, trip.owner, trip, equal_expense(300, bea, [bea, cam], description="Taxi")
    )
    preview = await api.get(trip.path("/ledger/settlement-preview"), headers=trip.owner.headers)
    assert preview.status_code == 200
    # Ann +600, Bea -300 + 150 = -150, Cam -300 - 150 = -450.
    assert preview.json() == [
        {
            "currency": "USD",
            "transfers": [
                {"from_participant_id": cam, "to_participant_id": ann, "amount_minor": 450},
                {"from_participant_id": bea, "to_participant_id": ann, "amount_minor": 150},
            ],
            "fund_payouts": [],
        }
    ]
    explained = await api.get(
        trip.path("/ledger/explanation"),
        params={"participant_id": bea, "currency": "USD", "limit": 1},
        headers=trip.members["Bea"].headers,
    )
    page = explained.json()
    assert [(e["kind"], e["amount_minor"], e["balance_after_minor"]) for e in page["entries"]] == [
        ("expense", -300, -300)
    ]
    assert page["entries"][0]["description"] == "Dinner"
    assert page["entries"][0]["expense_id"] == first["id"]
    rest = await api.get(
        trip.path("/ledger/explanation"),
        params={"participant_id": bea, "currency": "USD", "cursor": page["next_cursor"]},
        headers=trip.members["Bea"].headers,
    )
    assert [
        (e["description"], e["amount_minor"], e["balance_after_minor"])
        for e in rest.json()["entries"]
    ] == [("Taxi", 150, -150)]
    journal = await api.get(trip.path("/ledger/transactions"), headers=trip.owner.headers)
    assert [t["ledger_seq"] for t in journal.json()["items"]] == [1, 2]
    bad = await api.get(
        trip.path("/ledger/transactions"), params={"cursor": "x"}, headers=trip.owner.headers
    )
    assert bad.status_code == 422


async def test_people_who_left_can_still_settle_and_entities_sync(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, cam = trip.people["Ann"], trip.people["Cam"]
    await add_expense(api, trip.owner, trip, equal_expense(400, ann, [ann, cam]))
    removed = await api.delete(trip.path(f"/participants/{cam}"), headers=trip.owner.headers)
    assert removed.status_code in (200, 204)
    settlement_id = str(new_id())
    paid = await settle(api, trip.owner, trip, {**payment(cam, ann, 200), "id": settlement_id})
    assert paid.status_code == 201, paid.text
    items, _, _ = await pull_all(api, trip.members["Bea"], f"plan:{trip.plan_id}")
    synced = {(i["entity_type"], i["entity_id"]): i["data"] for i in items}
    assert synced[("settlement", settlement_id)] == paid.json()
    assert synced[("ledger", trip.plan_id)]["status"] == "settled"


async def test_editing_a_settled_expense_reopens_and_still_explains_exactly(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    partial = await settle(api, trip.members["Bea"], trip, payment(bea, ann, 100))
    assert (await ledger(api, trip.owner, trip))["status"] == "open"
    await settle(api, trip.members["Bea"], trip, payment(bea, ann, 200))
    assert (await ledger(api, trip.owner, trip))["status"] == "settled"
    revised = await api.put(
        trip.path(f"/expenses/{expense['id']}"),
        json=equal_expense(800, ann, [ann, bea]),
        headers=if_match(1, trip.owner),
    )
    assert revised.status_code == 200
    assert (await ledger(api, trip.owner, trip))["status"] == "reopened"
    await api.post(
        trip.path(f"/settlements/{partial.json()['id']}/reverse"),
        headers=if_match(1, trip.members["Bea"]),
    )
    balances = await ledger_balances(api, trip.owner, trip)
    assert balances == {(ann, "USD"): 200, (bea, "USD"): -200}
    explained = await api.get(
        trip.path("/ledger/explanation"),
        params={"participant_id": bea, "currency": "USD"},
        headers=trip.owner.headers,
    )
    entries = explained.json()["entries"]
    assert [e["kind"] for e in entries] == [
        "expense",
        "settlement",
        "settlement",
        "expense_reversal",
        "expense",
        "settlement_reversal",
    ]
    assert sum(e["amount_minor"] for e in entries) == entries[-1]["balance_after_minor"] == -200
    # Nothing was overwritten: both revisions and every posting remain.
    assert admin.scalar("SELECT count(*) FROM finance.expense_revisions") == 2
    assert admin.scalar("SELECT count(*) FROM finance.ledger_transactions") == 6
