"""Exports hold what each person can already sync, and never a secret."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import httpx
import pytest

from beluno.api import exports
from beluno.modules.iam import rate_limits
from beluno.modules.iam.rate_limits import RateLimit
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, add_expense, equal_expense, finance_plan, if_match
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

CODE = "RYO-9931-ZZ"


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


async def export(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, fmt: str
) -> httpx.Response:
    return await api.get(trip.path("/export"), params={"format": fmt}, headers=user.headers)


async def created(
    api: httpx.AsyncClient, user: SignedIn, trip: FinancePlan, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    response = await api.post(trip.path(path), json=body, headers=user.headers)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_the_csv_lists_every_expense_with_its_base_amount(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(
        api, trip.owner, trip, equal_expense(1_234, ann, [ann, bea], description="Ramen")
    )
    await add_expense(
        api,
        trip.owner,
        trip,
        equal_expense(
            3_000,
            bea,
            [ann, bea],
            currency="JPY",
            description='=HYPERLINK("http://x")',
            base_rate={"rate": "0.0067", "base_currency": "USD"},
        ),
    )
    response = await export(api, trip.members["Dan"], trip, "csv")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="beluno-plan-{trip.plan_id}.csv"'
    )
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [(r["description"], r["amount"], r["currency"]) for r in rows] == [
        ("Ramen", "12.34", "USD"),
        ('\'=HYPERLINK("http://x")', "3000", "JPY"),
    ]
    ramen, formula = rows
    assert (ramen["base_amount"], ramen["base_currency"], ramen["rate_source"]) == (
        "12.34",
        "USD",
        "identity",
    )
    assert ramen["paid_by"] == "Ann: 12.34"
    assert ramen["split"] in ("Ann: 6.17; Bea: 6.17", "Bea: 6.17; Ann: 6.17")
    assert (formula["base_amount"], formula["rate"]) == ("20.10", "0.0067")
    assert ramen["state"] == "active"
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events "
            "WHERE action = 'plan.exported' AND plan_id = %s",
            trip.plan_id,
        )
        == 1
    )


async def test_the_json_holds_what_sync_gives_and_no_secret(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, trip: FinancePlan
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann = trip.people["Ann"]
    await add_expense(api, owner, trip, equal_expense(900, ann, [ann, trip.people["Bea"]]))
    await created(
        api,
        owner,
        trip,
        "/bookings",
        {"kind": "lodging", "title": "Ryokan", "secrets": {"confirmation_code": CODE}},
    )
    await created(api, owner, trip, "/invites", {})
    mine = await created(api, dan, trip, "/packing", {"name": "Socks", "visibility": "private"})
    hers = await created(api, bea, trip, "/packing", {"name": "Pills", "visibility": "private"})
    await ok_role(api, trip, "Dan", "viewer")

    response = await export(api, dan, trip, "json")
    assert response.status_code == 200, response.text
    assert CODE not in response.text and "Pills" not in response.text
    document = response.json()
    assert (document["format"], document["version"], document["plan_id"]) == (
        "beluno.export",
        1,
        trip.plan_id,
    )
    entities = document["entities"]
    assert len(entities["expense"]) == 1 and len(entities["booking"]) == 1
    assert entities["booking"][0]["has_confirmation_code"] is True
    assert "plan_invite" not in entities  # managers only
    assert [row["id"] for row in entities["packing_item"]] == [mine["id"]]
    assert hers["id"] not in response.text

    managed = (await export(api, owner, trip, "json")).json()["entities"]
    # The two links Bea and Dan joined through, and the new one; never a token.
    assert len(managed["plan_invite"]) == 3
    assert not any("token" in invite for invite in managed["plan_invite"])

    stranger = await sign_in(api, identity_provider, name="Stranger")
    assert (await export(api, stranger, trip, "json")).status_code == 404
    assert (await export(api, owner, trip, "xml")).status_code == 422


async def ok_role(api: httpx.AsyncClient, trip: FinancePlan, name: str, role: str) -> None:
    response = await api.patch(
        trip.path(f"/participants/{trip.people[name]}"),
        json={"role": role},
        headers=if_match(1, trip.owner),
    )
    assert response.status_code == 200, response.text


async def test_an_account_export_covers_the_plans_it_is_still_in(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    admin: AdminDatabase,
) -> None:
    bea = trip.members["Bea"]
    other = await finance_plan(api, identity_provider, admin, members=())
    joined = await api.post(
        other.path("/invites"), json={"max_uses": 1}, headers=other.owner.headers
    )
    redeemed = await api.post(
        "/v1/invites/redeem", json={"token": joined.json()["token"]}, headers=bea.headers
    )
    assert redeemed.status_code == 200, redeemed.text
    left = await api.post(other.path("/leave"), headers=bea.headers)
    assert left.status_code == 204, left.text
    await created(api, bea, trip, "/packing", {"name": "Book", "visibility": "private"})

    response = await api.get("/v1/me/export", headers=bea.headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        f'attachment; filename="beluno-account-{bea.user_id}.json"'
    )
    document = json.loads(response.content)
    assert document["user_id"] == bea.user_id
    entities = document["entities"]
    assert [row["id"] for row in entities["user"]] == [bea.user_id]
    assert [row["name"] for row in entities["packing_item"]] == ["Book"]
    assert list(document["plans"]) == [trip.plan_id]
    assert document["plans"][trip.plan_id]["plan"][0]["id"] == trip.plan_id
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events "
            "WHERE action = 'account.exported' AND entity_id = %s",
            bea.user_id,
        )
        == 1
    )


async def test_every_page_counts_and_free_text_never_runs_as_a_formula(
    api: httpx.AsyncClient, trip: FinancePlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports, "PAGE_SIZE", 2)
    ann = trip.people["Ann"]
    evil = await created(api, trip.owner, trip, "/participants", {"placeholder_name": "=Evil"})
    voided = None
    for number in range(5):
        body = equal_expense(
            100 + number, evil["id"], [ann, evil["id"]], description=f"No {number}"
        )
        voided = await add_expense(api, trip.owner, trip, body)
    assert voided is not None
    gone = await api.post(
        trip.path(f"/expenses/{voided['id']}/void"), headers=if_match(1, trip.owner)
    )
    assert gone.status_code == 200, gone.text

    response = await export(api, trip.owner, trip, "csv")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["description"] for r in rows] == [f"No {n}" for n in range(5)]
    assert rows[0]["paid_by"] == "'=Evil: 1.00"
    assert [r["state"] for r in rows] == ["active"] * 4 + ["voided"]


async def test_guests_export_and_people_who_lost_access_do_not(
    api: httpx.AsyncClient, trip: FinancePlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, dan = trip.owner, trip.members["Dan"]
    invite = await created(api, owner, trip, "/invites", {})
    await created(
        api,
        owner,
        trip,
        "/bookings",
        {"kind": "other", "title": "Show", "secrets": {"private_notes": "Seat 14C, gate code 88"}},
    )
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": invite["token"], "display_name": "Gia", "device": {"platform": "web"}},
    )
    assert joined.status_code == 200, joined.text
    guest = signed_in_from(joined.json()["session"])
    as_guest = await export(api, guest, trip, "json")
    assert as_guest.status_code == 200, as_guest.text
    managed = await export(api, owner, trip, "json")
    for text in (as_guest.text, managed.text):
        assert invite["token"] not in text and "gate code" not in text

    removed = await api.delete(
        trip.path(f"/participants/{trip.people['Dan']}"), headers=owner.headers
    )
    assert removed.status_code == 204, removed.text
    assert (await export(api, dan, trip, "csv")).status_code == 404
    mine = (await api.get("/v1/me/export", headers=dan.headers)).json()
    assert mine["plans"] == {}

    monkeypatch.setattr(rate_limits, "EXPORTS_PER_USER", RateLimit("export:test", 1, 3_600))
    assert (await export(api, guest, trip, "csv")).status_code == 200
    limited = await export(api, guest, trip, "csv")
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"
