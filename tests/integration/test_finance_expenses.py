"""Expenses on the ledger: postings, revisions, voids, refunds, currencies, access, and sync."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest

from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.testkit.api_client import sign_in
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


def ids(trip: FinancePlan, *names: str) -> list[str]:
    return [trip.people[name] for name in names]


async def test_expense_posts_paid_minus_owed_and_syncs(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea, cam = ids(trip, "Ann", "Bea", "Cam")
    response = await api.post(
        trip.path("/expenses"),
        json=equal_expense(9000, ann, [ann, bea, cam]),
        headers=trip.owner.headers,
    )
    assert response.status_code == 201, response.text
    assert response.headers["ETag"] == '"1"'
    expense = response.json()
    assert expense["state"] == "active" and expense["version"] == 1
    revision = expense["revision"]
    assert revision["revision_number"] == 1 and revision["split_algorithm"] == "lr-v1"
    assert [s["owed_minor"] for s in revision["shares"]] == [3000, 3000, 3000]
    assert revision["payers"] == [{"participant_id": ann, "fund": False, "amount_minor": 9000}]
    assert revision["split"] == {"method": "equal", "participant_ids": [ann, bea, cam]}
    assert revision["base"] == {
        "currency": "USD",
        "amount_minor": 9000,
        "rate": None,
        "rate_source": "identity",
        "rate_as_of": None,
    }
    assert await ledger_balances(api, trip.owner, trip) == {
        (ann, "USD"): 6000,
        (bea, "USD"): -3000,
        (cam, "USD"): -3000,
    }
    fetched = await api.get(trip.path(f"/expenses/{expense['id']}"), headers=trip.owner.headers)
    assert fetched.json() == expense and fetched.headers["ETag"] == '"1"'
    listed = await api.get(trip.path("/expenses"), headers=trip.members["Bea"].headers)
    assert [item["id"] for item in listed.json()["items"]] == [expense["id"]]

    items, _, status = await pull_all(api, trip.members["Bea"], f"plan:{trip.plan_id}")
    assert status == "ok"
    by_type = {(item["entity_type"], item["entity_id"]): item for item in items}
    assert by_type[("expense", expense["id"])]["data"] == expense
    ledger_item = by_type[("ledger", trip.plan_id)]["data"]
    assert ledger_item["ledger_seq"] == 1 and ledger_item["status"] == "open"
    audit = admin.fetch(
        "SELECT action, metadata FROM sync_audit.audit_events WHERE entity_id = %s",
        expense["id"],
    )
    # Audit carries identifiers and versions only, never descriptions or amounts.
    assert audit == [("finance.expense_created", {"version": 1})]


async def test_revision_reverses_the_previous_one_and_keeps_history(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea, cam = ids(trip, "Ann", "Bea", "Cam")
    expense = await add_expense(api, trip.owner, trip, equal_expense(9000, ann, [ann, bea, cam]))
    path = trip.path(f"/expenses/{expense['id']}")
    missing = await api.put(path, json=equal_expense(100, ann, [bea]), headers=trip.owner.headers)
    assert missing.status_code == 428
    revised = await api.put(
        path,
        json=equal_expense(1000, bea, [ann, cam], description="Taxi"),
        headers=if_match(1, trip.owner),
    )
    assert revised.status_code == 200, revised.text
    body = revised.json()
    assert body["version"] == 2 and body["revision"]["revision_number"] == 2
    assert body["revision"]["description"] == "Taxi"
    assert await ledger_balances(api, trip.owner, trip) == {
        (ann, "USD"): -500,
        (bea, "USD"): 1000,
        (cam, "USD"): -500,
    }
    stale = await api.put(path, json=equal_expense(5, ann, [ann]), headers=if_match(1, trip.owner))
    assert stale.status_code == 412
    assert stale.json()["code"] == "VERSION_CONFLICT"
    assert stale.json()["current"] == body
    history = await api.get(path + "/revisions", headers=trip.owner.headers)
    assert [r["revision_number"] for r in history.json()] == [1, 2]
    assert [r["amount_minor"] for r in history.json()] == [9000, 1000]
    kinds = admin.fetch(
        "SELECT kind, ledger_seq FROM finance.ledger_transactions WHERE plan_id = %s "
        "ORDER BY ledger_seq",
        trip.plan_id,
    )
    assert kinds == [("expense", 1), ("expense_reversal", 2), ("expense", 3)]


async def test_refunds_follow_shares_and_voiding_reverses_everything(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea, cam = ids(trip, "Ann", "Bea", "Cam")
    expense = await add_expense(api, trip.owner, trip, equal_expense(9000, ann, [ann, bea, cam]))
    path = trip.path(f"/expenses/{expense['id']}")
    refund = await api.post(
        path + "/refunds",
        json={"amount_minor": 3001, "recipient": {"participant_id": ann}},
        headers=if_match(1, trip.owner),
    )
    assert refund.status_code == 200, refund.text
    refunded = refund.json()
    assert refunded["version"] == 2 and refunded["refunded_minor"] == 3001
    assert [s["amount_minor"] for s in refunded["refunds"][0]["shares"]] == [1001, 1000, 1000]
    # Ann paid 9000, owed 3000, got 3001 back and owes 1001 of it again.
    assert await ledger_balances(api, trip.owner, trip) == {
        (ann, "USD"): 6000 - 3001 + 1001,
        (bea, "USD"): -3000 + 1000,
        (cam, "USD"): -3000 + 1000,
    }
    too_much = await api.post(
        path + "/refunds",
        json={"amount_minor": 6000, "recipient": {"participant_id": ann}},
        headers=if_match(2, trip.owner),
    )
    assert too_much.status_code == 409 and too_much.json()["code"] == "REFUND_EXCEEDS_AMOUNT"
    below = await api.put(
        path, json=equal_expense(3000, ann, [ann, bea]), headers=if_match(2, trip.owner)
    )
    assert below.status_code == 409 and below.json()["code"] == "REFUND_EXCEEDS_AMOUNT"
    explicit = await api.post(
        path + "/refunds",
        json={
            "amount_minor": 500,
            "recipient": {"participant_id": bea},
            "shares": [{"participant_id": bea, "amount_minor": 500}],
        },
        headers=if_match(2, trip.owner),
    )
    assert explicit.status_code == 200, explicit.text
    voided = await api.post(path + "/void", headers=if_match(3, trip.owner))
    assert voided.status_code == 200, voided.text
    assert voided.json()["state"] == "voided" and voided.json()["voided_at"] is not None
    assert all(item["reversed"] for item in voided.json()["refunds"])
    assert voided.json()["refunded_minor"] == 0
    assert await ledger_balances(api, trip.owner, trip) == {}
    again = await api.put(path, json=equal_expense(10, ann, [ann]), headers=if_match(4, trip.owner))
    assert again.status_code == 409 and again.json()["code"] == "INVALID_STATE_TRANSITION"


async def test_split_methods_and_validation_codes(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea, cam = ids(trip, "Ann", "Bea", "Cam")
    itemized = await add_expense(
        api,
        trip.owner,
        trip,
        {
            "description": "Pizza night",
            "occurred_on": "2026-10-06",
            "amount_minor": 7000,
            "currency": "USD",
            "payers": [
                {"participant_id": ann, "amount_minor": 5000},
                {"participant_id": bea, "amount_minor": 2000},
            ],
            "split": {
                "method": "itemized",
                "items": [
                    {"label": "Large", "amount_minor": 3000, "participant_ids": [ann]},
                    {"amount_minor": 1000, "participant_ids": [bea, cam]},
                    {
                        "amount_minor": 2000,
                        "participant_ids": [ann, bea, cam],
                        "weights": [2, 1, 1],
                    },
                ],
                "extras": [{"label": "Tip", "amount_minor": 1000}],
            },
        },
    )
    assert [s["owed_minor"] for s in itemized["revision"]["shares"]] == [4667, 1167, 1166]
    assert await ledger_balances(api, trip.owner, trip) == {
        (ann, "USD"): 333,
        (bea, "USD"): 833,
        (cam, "USD"): -1166,
    }
    weighted = equal_expense(1001, ann, [])
    weighted["split"] = {
        "method": "shares",
        "shares": [
            {"participant_id": ann, "weight": 1},
            {"participant_id": bea, "weight": 2},
            {"participant_id": cam, "weight": 3},
        ],
    }
    created = await add_expense(api, trip.owner, trip, weighted)
    assert [s["owed_minor"] for s in created["revision"]["shares"]] == [167, 334, 500]

    other = await finance_plan_participant(api, trip, admin)
    cases: list[tuple[dict[str, Any], int, str]] = [
        (equal_expense(0, ann, [ann]), 422, "AMOUNT_OUT_OF_RANGE"),
        (equal_expense(10**12 + 1, ann, [ann]), 422, "AMOUNT_OUT_OF_RANGE"),
        (equal_expense(100, ann, [ann], currency="XYZ"), 422, "CURRENCY_NOT_SUPPORTED"),
        (
            {
                **equal_expense(100, ann, [ann]),
                "payers": [{"participant_id": ann, "amount_minor": 90}],
            },
            422,
            "SPLIT_INVALID",
        ),
        (equal_expense(100, ann, [ann, other]), 422, "PARTICIPANT_NOT_ELIGIBLE"),
        (equal_expense(100, str(uuid4()), [ann]), 422, "PARTICIPANT_NOT_ELIGIBLE"),
        ({**equal_expense(100, ann, [ann]), "amount_minor": 1.5}, 422, "VALIDATION_FAILED"),
    ]
    for body, status, code in cases:
        response = await api.post(trip.path("/expenses"), json=body, headers=trip.owner.headers)
        assert (response.status_code, response.json()["code"]) == (status, code), body


async def finance_plan_participant(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> str:
    """A participant of somebody else's plan."""

    outsider = trip.members["Bea"]
    response = await api.post(
        "/v1/plans",
        json={
            "title": "Elsewhere",
            "base_currency": "USD",
            "participants": [{"placeholder_name": "X"}],
        },
        headers=outsider.headers,
    )
    assert response.status_code == 201, response.text
    people = await api.get(
        f"/v1/plans/{response.json()['id']}/participants", headers=outsider.headers
    )
    return next(p["id"] for p in people.json() if p["display_name"] == "X")


async def test_foreign_currency_uses_exponents_and_labelled_base_snapshots(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = ids(trip, "Ann", "Bea")
    with_rate = await add_expense(
        api,
        trip.owner,
        trip,
        equal_expense(
            1000,
            ann,
            [ann, bea],
            currency="JPY",
            base_rate={"rate": "0.0067", "source": "estimated"},
        ),
    )
    base = with_rate["revision"]["base"]
    assert (base["currency"], base["amount_minor"], base["rate"], base["rate_source"]) == (
        "USD",
        670,
        "0.0067",
        "estimated",
    )
    kwd = await add_expense(
        api, trip.owner, trip, equal_expense(10_000, bea, [ann, bea], currency="KWD")
    )
    assert kwd["revision"]["base"]["amount_minor"] is None
    assert kwd["revision"]["base"]["rate_source"] is None
    balances = await ledger_balances(api, trip.owner, trip)
    # Currencies are never netted: each keeps its own balances.
    assert balances == {
        (ann, "JPY"): 500,
        (bea, "JPY"): -500,
        (ann, "KWD"): -5000,
        (bea, "KWD"): 5000,
    }
    locked = await api.patch(
        trip.path(), json={"base_currency": "EUR"}, headers=if_match(1, trip.owner)
    )
    assert locked.status_code == 409 and locked.json()["code"] == "BASE_CURRENCY_LOCKED"


async def test_currency_catalog_and_plan_currencies_are_validated(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    catalog = await api.get("/v1/currencies", headers=trip.owner.headers)
    exponents = {row["code"]: row["exponent"] for row in catalog.json()}
    assert (exponents["JPY"], exponents["USD"], exponents["KWD"]) == (0, 2, 3)
    unknown = await api.post(
        "/v1/plans", json={"title": "X", "base_currency": "XYZ"}, headers=trip.owner.headers
    )
    assert unknown.status_code == 422 and unknown.json()["code"] == "CURRENCY_NOT_SUPPORTED"
    group = await api.patch(
        f"/v1/groups/{trip.group_id}",
        json={"default_currency": "ZZZ"},
        headers=if_match(1, trip.owner),
    )
    assert group.status_code == 422 and group.json()["code"] == "CURRENCY_NOT_SUPPORTED"
    # Without finance data the base currency may still change.
    fresh = await api.post(
        "/v1/plans", json={"title": "Fresh", "base_currency": "USD"}, headers=trip.owner.headers
    )
    moved = await api.patch(
        f"/v1/plans/{fresh.json()['id']}",
        json={"base_currency": "EUR"},
        headers=if_match(1, trip.owner),
    )
    assert moved.status_code == 200 and moved.json()["base_currency"] == "EUR"


async def test_only_the_creator_or_a_manager_changes_an_expense(
    api: httpx.AsyncClient, trip: FinancePlan, identity_provider: IdentityProviderStub
) -> None:
    ann, bea, cam = ids(trip, "Ann", "Bea", "Cam")
    bea_user = trip.members["Bea"]
    mine = await add_expense(api, bea_user, trip, equal_expense(600, bea, [bea, cam]))
    theirs = await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    own = await api.post(trip.path(f"/expenses/{mine['id']}/void"), headers=if_match(1, bea_user))
    assert own.status_code == 200
    other = await api.post(
        trip.path(f"/expenses/{theirs['id']}/void"), headers=if_match(1, bea_user)
    )
    assert other.status_code == 403
    managed = await api.post(
        trip.path(f"/expenses/{mine['id']}/refunds"),
        json={"amount_minor": 1, "recipient": {"participant_id": bea}},
        headers=if_match(2, trip.owner),
    )
    # The manager may act on Bea's expense; it is voided, so the state answers.
    assert managed.status_code == 409

    viewer_role = await api.patch(
        trip.path(f"/participants/{bea}"), json={"role": "viewer"}, headers=if_match(1, trip.owner)
    )
    assert viewer_role.status_code == 200, viewer_role.text
    denied = await api.post(
        trip.path("/expenses"), json=equal_expense(100, bea, [bea]), headers=bea_user.headers
    )
    assert denied.status_code == 403
    readable = await api.get(trip.path("/ledger"), headers=bea_user.headers)
    assert readable.status_code == 200

    outsider = await sign_in(api, identity_provider, name="Outsider")
    for path in ("/ledger", "/expenses", f"/expenses/{theirs['id']}"):
        hidden = await api.get(trip.path(path), headers=outsider.headers)
        assert hidden.status_code == 404, path


async def test_group_readers_get_no_finance_data(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    ann, bea = ids(trip, "Ann", "Bea")
    await add_expense(api, trip.owner, trip, equal_expense(400, ann, [ann, bea]))
    visible = await api.patch(
        trip.path(), json={"visibility": "group"}, headers=if_match(1, trip.owner)
    )
    assert visible.status_code == 200, visible.text
    dan = trip.members["Dan"]
    removed = await api.delete(
        trip.path(f"/participants/{trip.people['Dan']}"), headers=trip.owner.headers
    )
    assert removed.status_code in (200, 204), removed.text
    assert (await api.get(trip.path(), headers=dan.headers)).status_code == 200
    assert (await api.get(trip.path("/ledger"), headers=dan.headers)).status_code == 403
    items, _, status = await pull_all(api, dan, f"plan:{trip.plan_id}")
    assert status == "ok"
    assert {item["entity_type"] for item in items} & {"ledger", "expense"} == set()


async def test_idempotent_create_replays_one_revision(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea = ids(trip, "Ann", "Bea")
    body = equal_expense(500, ann, [ann, bea])
    headers = {**trip.owner.headers, "Idempotency-Key": "lost-ack-1"}
    first = await api.post(trip.path("/expenses"), json=body, headers=headers)
    second = await api.post(trip.path("/expenses"), json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotency-Replayed"] == "true"
    assert first.json() == second.json()
    assert admin.scalar("SELECT count(*) FROM finance.expense_revisions") == 1
    assert admin.scalar("SELECT count(*) FROM finance.ledger_transactions") == 1
    reused = await api.post(
        trip.path("/expenses"), json=equal_expense(501, ann, [ann, bea]), headers=headers
    )
    assert reused.status_code == 409 and reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_push_creates_and_conflicts_with_the_current_expense(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = ids(trip, "Ann", "Bea")
    expense_id = str(new_id())
    create_op, revise_op, stale_op = str(new_id()), str(new_id()), str(new_id())

    def op(command: str, operation_id: str, payload: dict[str, Any], version: int | None) -> dict:
        target = {"plan_id": trip.plan_id}
        if command != "expense.create":
            target["expense_id"] = expense_id
        return {
            "operation_id": operation_id,
            "command": command,
            "schema_version": 1,
            "target": target,
            "expected_version": version,
            "depends_on": [],
            "payload": payload,
        }

    response = await api.post(
        "/v1/sync/push",
        json={
            "operations": [
                op(
                    "expense.create",
                    create_op,
                    {**equal_expense(800, ann, [ann, bea]), "id": expense_id},
                    None,
                ),
                op("expense.revise", revise_op, equal_expense(900, bea, [ann, bea]), 1),
                op("expense.revise", stale_op, equal_expense(100, ann, [ann]), 1),
            ]
        },
        headers=trip.owner.headers,
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [r["outcome"] for r in results] == ["applied", "applied", "conflict"]
    assert results[2]["problem"]["current"]["version"] == 2
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): -450, (bea, "USD"): 450}


async def test_fund_payer_needs_money_in_the_fund(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann = trip.people["Ann"]
    body = equal_expense(300, ann, [ann])
    body["payers"] = [{"fund": True, "amount_minor": 300}]
    response = await api.post(trip.path("/expenses"), json=body, headers=trip.owner.headers)
    assert response.status_code == 409 and response.json()["code"] == "FUND_INSUFFICIENT"


async def test_finance_kill_switch_keeps_reads(
    live_settings: Settings, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    from beluno.api.main import create_app
    from beluno.db.session import Database

    settings = live_settings.model_copy(update={"finance_writes_enabled": False})
    database = Database(settings)
    app = create_app(
        settings=settings, database=database, identity_verifier=identity_provider.verifier(settings)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as api:
        trip = await finance_plan(api, identity_provider, admin)
        ann = trip.people["Ann"]
        refused = await api.post(
            trip.path("/expenses"), json=equal_expense(100, ann, [ann]), headers=trip.owner.headers
        )
        ledger = await api.get(trip.path("/ledger"), headers=trip.owner.headers)
    await database.close()
    assert refused.status_code == 503 and refused.json()["code"] == "FEATURE_DISABLED"
    assert ledger.status_code == 200 and ledger.json()["balances"] == []


async def test_removed_participants_keep_history_but_join_no_new_entries(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea, cam = ids(trip, "Ann", "Bea", "Cam")
    expense = await add_expense(api, trip.owner, trip, equal_expense(900, ann, [ann, cam]))
    other = await add_expense(api, trip.owner, trip, equal_expense(400, ann, [ann, bea]))
    removed = await api.delete(trip.path(f"/participants/{cam}"), headers=trip.owner.headers)
    assert removed.status_code in (200, 204), removed.text
    fresh = await api.post(
        trip.path("/expenses"), json=equal_expense(100, ann, [cam]), headers=trip.owner.headers
    )
    assert fresh.json()["code"] == "PARTICIPANT_NOT_ELIGIBLE"
    kept = await api.put(
        trip.path(f"/expenses/{expense['id']}"),
        json=equal_expense(1200, ann, [ann, bea, cam]),
        headers=if_match(1, trip.owner),
    )
    assert kept.status_code == 200, kept.text
    added = await api.put(
        trip.path(f"/expenses/{other['id']}"),
        json=equal_expense(400, ann, [ann, cam]),
        headers=if_match(1, trip.owner),
    )
    assert added.status_code == 422 and added.json()["code"] == "PARTICIPANT_NOT_ELIGIBLE"
    # Cam still owes their share; history and balances survive the removal.
    assert (await ledger_balances(api, trip.owner, trip))[(cam, "USD")] == -400
