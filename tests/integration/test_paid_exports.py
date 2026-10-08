"""The accounting CSV and the PDF trip report: paid, complete, and adding up."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pypdf import PdfReader

from beluno.api import trip_report
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    exercise_money_and_members,
    finance_plan,
    if_match,
    ledger_balances,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.stores import pass_for

pytestmark = pytest.mark.integration

EXPONENTS = {"USD": 2, "EUR": 2, "JPY": 0, "VND": 0}


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Đạt"))


async def export(api: httpx.AsyncClient, trip: FinancePlan, kind: str) -> httpx.Response:
    return await api.get(trip.path(f"/export?format={kind}"), headers=trip.owner.headers)


def rows(response: httpx.Response) -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def sums(lines: list[dict[str, str]]) -> dict[tuple[str | None, str], int]:
    totals: dict[tuple[str | None, str], int] = defaultdict(int)
    for line in lines:
        minor = Decimal(line["amount"]).scaleb(EXPONENTS[line["currency"]])
        totals[(line["person_id"] or None, line["currency"])] += int(minor)
    return {key: value for key, value in totals.items() if value}


async def ok(response: httpx.Response, status: int = 201) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def test_paid_exports_need_a_trip_pass(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    admin: AdminDatabase,
    identity_provider: IdentityProviderStub,
) -> None:
    for kind in ("accounting", "pdf"):
        refused = await export(api, trip, kind)
        assert refused.status_code == 403 and refused.json()["code"] == "UPGRADE_REQUIRED"
    # Free exports stay free.
    assert (await export(api, trip, "csv")).status_code == 200
    # Hangouts are always free, but the report is for trips.
    hangout = await finance_plan(api, identity_provider, admin, plan_type="hangout")
    assert (await export(api, hangout, "accounting")).status_code == 200
    report = await export(api, hangout, "pdf")
    assert report.status_code == 409 and report.json()["code"] == "NOT_AVAILABLE_FOR_HANGOUT"
    # Everyone on an unlocked trip may export, not only the buyer.
    pass_for(admin, trip)
    member = await api.get(
        trip.path("/export?format=accounting"), headers=trip.members["Bea"].headers
    )
    assert member.status_code == 200


async def test_the_accounting_lines_add_up_to_the_balances(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    pass_for(admin, trip)
    owner = trip.owner
    ann, bea, dat, cam = (trip.people[name] for name in ("Ann", "Bea", "Đạt", "Cam"))
    dinner = await add_expense(
        api, owner, trip, equal_expense(900, ann, [ann, bea, cam], description="=SUM(A1)")
    )
    await add_expense(api, owner, trip, equal_expense(3_000, bea, [ann, bea, dat], currency="JPY"))
    voided = await add_expense(api, owner, trip, equal_expense(5_000, dat, [ann, dat]))
    await ok(
        await api.post(trip.path(f"/expenses/{voided['id']}/void"), headers=if_match(1, owner)),
        200,
    )
    await ok(
        await api.post(
            trip.path(f"/expenses/{dinner['id']}/refunds"),
            json={"amount_minor": 300, "recipient": {"participant_id": ann}},
            headers=if_match(1, owner),
        ),
        200,
    )
    for kind, debtor, creditor, amount in (
        ("settlements", cam, ann, 100),
        ("waivers", bea, ann, 50),
    ):
        await ok(
            await api.post(
                trip.path(f"/{kind}"),
                json={
                    "from_participant_id"
                    if kind == "settlements"
                    else "debtor_participant_id": debtor,
                    "to_participant_id"
                    if kind == "settlements"
                    else "creditor_participant_id": creditor,
                    "currency": "USD",
                    "amount_minor": amount,
                    "occurred_on": "2026-10-07",
                },
                headers=owner.headers,
            )
        )
    for movement, amount in (("contributions", 1_000), ("withdrawals", 200)):
        await ok(
            await api.post(
                trip.path(f"/fund/{movement}"),
                json={
                    "participant_id": dat,
                    "currency": "USD",
                    "amount_minor": amount,
                    "occurred_on": "2026-10-07",
                },
                headers=owner.headers,
            )
        )
    kitty_paid = equal_expense(600, ann, [bea, dat], description="Taxi")
    kitty_paid["payers"] = [{"fund": True, "amount_minor": 600}]
    await add_expense(api, owner, trip, kitty_paid)

    lines = rows(await export(api, trip, "accounting"))
    assert sums(lines) == await ledger_balances(api, owner, trip)
    entries = {line["entry"] for line in lines}
    assert entries >= {
        "expense",
        "expense_reversal",
        "refund",
        "settlement",
        "waiver",
        "fund_contribution",
        "fund_withdrawal",
    }
    # The voided expense was posted and then reversed: it nets to nothing.
    assert (
        sum(Decimal(line["amount"]) for line in lines if line["reference_id"] == voided["id"]) == 0
    )
    assert any(line["description"] == "'=SUM(A1)" for line in lines)  # kept as text
    assert {line["person"] for line in lines} >= {"Đạt", "Kitty"}


async def test_consolidations_keep_the_lines_adding_up(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    pass_for(admin, trip)
    owner = trip.owner
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    await add_expense(api, owner, trip, equal_expense(900, ann, [ann, bea, cam]))
    await add_expense(api, owner, trip, equal_expense(3_000, ann, [ann, bea, cam], currency="JPY"))
    await ok(
        await api.post(
            trip.path("/ledger/consolidations"),
            json={"base_currency": "USD", "rates": [{"currency": "JPY", "rate": "0.0067"}]},
            headers=owner.headers,
        )
    )
    lines = rows(await export(api, trip, "accounting"))
    assert sums(lines) == await ledger_balances(api, owner, trip)
    assert any(line["entry"] == "conversion" for line in lines)


async def test_every_money_and_member_feature_adds_up(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await exercise_money_and_members(api, identity_provider, admin, memo="Owner only")
    pass_for(admin, trip)
    lines = rows(await export(api, trip, "accounting"))
    assert sums(lines) == await ledger_balances(api, trip.owner, trip)
    # Corrections are there, without the owner's memo.
    assert any(line["entry"] == "correction" for line in lines)
    assert all("Owner only" not in ",".join(line.values()) for line in lines)


async def test_the_trip_report_shows_the_money(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    admin: AdminDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trip_report, "MAX_EXPENSE_ROWS", 1)
    pass_for(admin, trip)
    owner = trip.owner
    ann, bea, dat = (trip.people[name] for name in ("Ann", "Bea", "Đạt"))
    await ok(
        await api.patch(
            trip.path(),
            json={"title": "Đà Lạt mùa hoa"},
            headers=if_match(1, owner),
        ),
        200,
    )
    await add_expense(
        api, owner, trip, equal_expense(1_200, ann, [ann, bea, dat], description="Bánh căn")
    )
    await add_expense(api, owner, trip, equal_expense(300, bea, [ann, bea], description="ข้าวผัด"))
    report = await export(api, trip, "pdf")
    assert report.status_code == 200, report.text
    assert report.headers["content-type"] == "application/pdf"
    assert report.headers["content-disposition"].endswith('.pdf"')
    assert report.content.startswith(b"%PDF")
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(report.content)).pages)
    for expected in ("Đà Lạt mùa hoa", "Bánh căn", "Đạt", "12.00 USD", "To settle up", "Food"):
        assert expected in text, expected
    # One row shown, the other counted; script the font lacks shows as "?".
    assert "And 1 more expenses" in text
    assert trip_report.printable("ข้าว Phở") == "???? Phở"
