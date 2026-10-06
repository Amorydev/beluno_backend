"""Settling everything in the base currency at frozen rates, and undoing it."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg
import pytest

from beluno.config import Settings
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

JPY_IN_USD = {"currency": "JPY", "rate": "0.0067"}


async def consolidate(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, *rates: dict[str, Any]
) -> httpx.Response:
    return await api.post(
        trip.path("/ledger/consolidations"), json={"rates": list(rates)}, headers=user.headers
    )


async def pay(
    api: httpx.AsyncClient, trip: FinancePlan, debtor: str, creditor: str, amount: int
) -> dict[str, Any]:
    response = await api.post(
        trip.path("/settlements"),
        json={
            "from_participant_id": debtor,
            "to_participant_id": creditor,
            "currency": "USD",
            "amount_minor": amount,
            "occurred_on": "2027-03-25",
        },
        headers=trip.owner.headers,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_everything_moves_into_the_base_currency_and_back(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    owner = trip.owner
    await add_expense(api, owner, trip, equal_expense(900, ann, [ann, bea, cam]))
    await add_expense(api, owner, trip, equal_expense(3_000, ann, [ann, bea, cam], currency="JPY"))
    before = await ledger_balances(api, owner, trip)
    assert before == {
        (ann, "USD"): 600,
        (bea, "USD"): -300,
        (cam, "USD"): -300,
        (ann, "JPY"): 2_000,
        (bea, "JPY"): -1_000,
        (cam, "JPY"): -1_000,
    }

    # Only managers, one rate per open currency, and an empty kitty.
    assert (await consolidate(api, trip.members["Bea"], trip, JPY_IN_USD)).status_code == 403
    assert (await consolidate(api, owner, trip)).status_code == 422
    eur = {"currency": "EUR", "rate": "1.1"}
    assert (await consolidate(api, owner, trip, JPY_IN_USD, eur)).status_code == 422
    kitty = {"participant_id": ann, "currency": "JPY", "amount_minor": 500}
    put_in = await api.post(
        trip.path("/fund/contributions"),
        json={**kitty, "occurred_on": "2027-03-20"},
        headers=owner.headers,
    )
    assert put_in.status_code == 201
    not_empty = await consolidate(api, owner, trip, JPY_IN_USD)
    assert not_empty.status_code == 409 and not_empty.json()["code"] == "FUND_NOT_EMPTY"
    paid_out = await api.post(
        trip.path("/fund/withdrawals"),
        json={**kitty, "occurred_on": "2027-03-21"},
        headers=owner.headers,
    )
    assert paid_out.status_code == 201

    created = await consolidate(api, owner, trip, JPY_IN_USD)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["state"], body["base_currency"], body["version"]) == ("active", "USD", 1)
    assert [(rate["currency"], rate["rate"]) for rate in body["rates"]] == [("JPY", "0.0067")]
    # 2,000 JPY at 0.67 cents each is 1,340 cents, shared on each side.
    lines = {
        (line["participant_id"], line["amount_minor"], line["base_amount_minor"])
        for line in body["lines"]
    }
    assert lines == {(ann, 2_000, 1_340), (bea, -1_000, -670), (cam, -1_000, -670)}
    consolidated = {(ann, "USD"): 1_940, (bea, "USD"): -970, (cam, "USD"): -970}
    assert await ledger_balances(api, owner, trip) == consolidated
    preview = await api.get(trip.path("/ledger/settlement-preview"), headers=owner.headers)
    assert [row["currency"] for row in preview.json() if row["transfers"]] == ["USD"]
    journal = await api.get(trip.path("/ledger/transactions"), headers=owner.headers)
    conversion = next(row for row in journal.json()["items"] if row["kind"] == "conversion")
    assert conversion["consolidation_id"] == body["id"]
    for currency in ("USD", "JPY"):
        assert (
            sum(p["amount_minor"] for p in conversion["postings"] if p["currency"] == currency) == 0
        )

    nothing_left = await consolidate(api, owner, trip, JPY_IN_USD)
    assert nothing_left.status_code == 409

    # Reversal restores the per-currency balances exactly.
    path = trip.path(f"/ledger/consolidations/{body['id']}/reverse")
    reversed_ = await api.post(path, headers=if_match(1, owner))
    assert reversed_.status_code == 200, reversed_.text
    assert reversed_.json()["state"] == "reversed"
    assert await ledger_balances(api, owner, trip) == before
    assert (await api.post(path, headers=if_match(2, owner))).status_code == 409

    # Once someone pays after a consolidation, it stays until that payment is reversed.
    again = (await consolidate(api, owner, trip, JPY_IN_USD)).json()
    payment = await pay(api, trip, bea, ann, 970)
    again_path = trip.path(f"/ledger/consolidations/{again['id']}/reverse")
    settled = await api.post(again_path, headers=if_match(1, owner))
    assert settled.status_code == 409 and settled.json()["code"] == "CONSOLIDATION_SETTLED"
    undone = await api.post(
        trip.path(f"/settlements/{payment['id']}/reverse"), headers=if_match(1, owner)
    )
    assert undone.status_code == 200, undone.text
    assert (await api.post(again_path, headers=if_match(1, owner))).status_code == 200
    assert await ledger_balances(api, owner, trip) == before

    items, _, _ = await pull_all(api, trip.members["Bea"], f"plan:{trip.plan_id}")
    synced = {
        item["entity_id"]: item["data"]["state"]
        for item in items
        if item["entity_type"] == "consolidation"
    }
    assert synced == {body["id"]: "reversed", again["id"]: "reversed"}
    assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", trip.plan_id) == []


async def test_the_database_holds_a_conversion_to_its_lines(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    live_settings: Settings,
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(10, ann, [ann, bea], currency="JPY"))
    await add_expense(api, trip.owner, trip, equal_expense(10, ann, [ann, bea]))
    accounts = dict(
        ((row[0], row[1]), str(row[2]))
        for row in admin.fetch(
            "SELECT participant_id::text, currency, id FROM finance.ledger_accounts "
            "WHERE plan_id = %s AND participant_id IS NOT NULL",
            trip.plan_id,
        )
    )
    consolidation, snapshot, tx = str(new_id()), str(new_id()), str(new_id())
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT INTO finance.consolidations (id, plan_id, base_currency, state, "
            "created_by_user_id, created_at, version, updated_at) "
            "VALUES (%s, %s, 'USD', 'active', %s, now(), 1, now())",
            (consolidation, trip.plan_id, trip.owner.user_id),
        ),
        (
            "INSERT INTO finance.fx_snapshots (id, plan_id, base_currency, quote_currency, rate, "
            "source, rounding_mode, as_of, created_by_user_id, created_at) "
            "VALUES (%s, %s, 'JPY', 'USD', 0.6, 'manual', 'half_even', now(), %s, now())",
            (snapshot, trip.plan_id, trip.owner.user_id),
        ),
        (
            "INSERT INTO finance.consolidation_rates (consolidation_id, plan_id, currency, "
            "fx_snapshot_id) VALUES (%s, %s, 'JPY', %s)",
            (consolidation, trip.plan_id, snapshot),
        ),
        (
            "INSERT INTO finance.consolidation_lines (consolidation_id, plan_id, currency, "
            "participant_id, amount_minor, base_amount_minor) "
            "VALUES (%s, %s, 'JPY', %s, 5, 3), (%s, %s, 'JPY', %s, -5, -3)",
            (consolidation, trip.plan_id, ann, consolidation, trip.plan_id, bea),
        ),
        (
            "UPDATE finance.plan_ledger_heads SET ledger_seq = ledger_seq + 1, "
            "version = version + 1 WHERE plan_id = %s",
            (trip.plan_id,),
        ),
        (
            "INSERT INTO finance.ledger_transactions (id, plan_id, ledger_seq, kind, "
            "consolidation_id, created_at) SELECT %s, %s, ledger_seq, 'conversion', %s, now() "
            "FROM finance.plan_ledger_heads WHERE plan_id = %s",
            (tx, trip.plan_id, consolidation, trip.plan_id),
        ),
        (
            # Zero-sum in each currency, but 4 cents instead of the 3 the lines say.
            "INSERT INTO finance.ledger_postings (transaction_id, account_id, plan_id, "
            "currency, amount_minor) VALUES (%s, %s, %s, 'JPY', -5), (%s, %s, %s, 'JPY', 5), "
            "(%s, %s, %s, 'USD', 4), (%s, %s, %s, 'USD', -4)",
            (
                tx,
                accounts[(ann, "JPY")],
                trip.plan_id,
                tx,
                accounts[(bea, "JPY")],
                trip.plan_id,
                tx,
                accounts[(ann, "USD")],
                trip.plan_id,
                tx,
                accounts[(bea, "USD")],
                trip.plan_id,
            ),
        ),
    ]
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.CheckViolation) as error,
        connection.transaction(),
    ):
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (trip.owner.user_id,))
        for statement, params in statements:
            connection.execute(statement, params)
    assert "postings do not match their source entry" in str(error.value)
