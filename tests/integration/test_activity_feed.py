"""The activity feed: what changed, for whom, atomically, and without free text."""

from __future__ import annotations

import json
from typing import Any

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.modules.activity.events import SUMMARY_KEYS
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    join_with_invite,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

SECRET = "Ramen at Ichiran 4F"


async def feed(api: httpx.AsyncClient, user: SignedIn, scope: str) -> list[dict[str, Any]]:
    items, _, status = await pull_all(api, user, scope)
    assert status == "ok"
    return [item["data"] for item in items if item["entity_type"] == "activity_event"]


async def test_money_member_and_plan_changes_read_as_a_feed(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin)
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    owner = trip.owner
    expense = await add_expense(
        api,
        owner,
        trip,
        equal_expense(900, ann, [ann, bea, cam], description=SECRET, notes="Locker code 4471"),
    )
    edited = await api.put(
        trip.path(f"/expenses/{expense['id']}"),
        json=equal_expense(1_200, ann, [ann, bea, cam], description="Dinner"),
        headers=if_match(1, owner),
    )
    assert edited.status_code == 200, edited.text
    paid = await api.post(
        trip.path("/settlements"),
        json={
            "from_participant_id": bea,
            "to_participant_id": ann,
            "currency": "USD",
            "amount_minor": 400,
            "occurred_on": "2026-10-07",
        },
        headers=owner.headers,
    )
    assert paid.status_code == 201
    moved = await api.post(
        trip.path("/state"), json={"state": "active"}, headers=if_match(1, owner)
    )
    assert moved.status_code == 200, moved.text
    rejected = await api.post(
        trip.path("/expenses"),
        json=equal_expense(100, ann, [ann], currency="XYZ"),
        headers=owner.headers,
    )
    assert rejected.status_code == 422

    events = await feed(api, trip.members["Bea"], f"plan:{trip.plan_id}")
    by_type = {event["type"]: event for event in events}
    assert [event["type"] for event in events] == [
        "plan.created",
        "member.joined",  # Cam, the placeholder
        "member.joined",  # Bea, through the invite
        "expense.added",
        "expense.edited",
        "payment.recorded",
        "plan.state_changed",
    ]
    assert by_type["expense.added"]["summary"] == {
        "amount_minor": 900,
        "currency": "USD",
        "category": "food",
    }
    assert by_type["expense.added"]["actor_user_id"] == owner.user_id
    assert by_type["expense.added"]["entity_id"] == expense["id"]
    assert by_type["expense.edited"]["summary"] == {
        "fields": ["amount", "description", "notes"],
        "amount_minor": 1_200,
        "previous_amount_minor": 900,
        "currency": "USD",
        "previous_currency": "USD",
    }
    assert by_type["payment.recorded"]["summary"] == {
        "from_participant_id": bea,
        "to_participant_id": ann,
        "amount_minor": 400,
        "currency": "USD",
    }
    assert by_type["plan.state_changed"]["summary"] == {
        "state": "active",
        "previous_state": "planning",
    }
    # A refused command left nothing behind, and no summary carries free text.
    assert admin.scalar("SELECT count(*) FROM activity.events") == len(events)
    for event in events:
        assert set(event["summary"]) <= SUMMARY_KEYS
    stored = json.dumps(admin.fetch("SELECT summary FROM activity.events"))
    assert SECRET not in stored and "4471" not in stored and "Ann" not in stored


async def test_removed_people_lose_the_feed_and_cannot_write_into_it(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    live_settings: Settings,
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    member = await sign_in(api, identity_provider, name="Minh")
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Karaoke", "base_currency": "VND"},
            headers=owner.headers,
        )
    ).json()
    row = await join_with_invite(api, owner, plan["id"], member)
    assert len(await feed(api, member, f"plan:{plan['id']}")) == 2
    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{row['id']}", headers=owner.headers
    )
    assert removed.status_code == 204
    pulled = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": f"plan:{plan['id']}", "cursor": None}]},
        headers=member.headers,
    )
    assert pulled.json()["scopes"][0]["status"] == "unavailable"
    owner_feed = await feed(api, owner, f"plan:{plan['id']}")
    assert owner_feed[-1]["type"] == "member.removed"
    assert owner_feed[-1]["summary"] == {"participant_id": row["id"]}

    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    forged = [
        {
            "id": str(new_id()),
            "scope_type": "plan",
            "scope_id": plan["id"],
            "plan_id": plan["id"],
            "type": "expense.added",
            "entity_type": "expense",
            "entity_id": str(new_id()),
            "summary": {},
            "occurred_at": "2026-10-07T00:00:00Z",
        }
    ]
    outsider = await sign_in(api, identity_provider, name="Outsider")
    with psycopg.connect(dsn) as connection:
        with connection.transaction():
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (member.user_id,))
            seen = connection.execute("SELECT count(*) FROM activity.events").fetchone()
            assert seen == (0,)
        for statement, params in (
            ("SELECT activity.append_events(%s::jsonb)", (json.dumps(forged),)),
            (
                "INSERT INTO activity.events (id, scope_type, scope_id, plan_id, type, "
                "entity_type, entity_id, summary, occurred_at) VALUES (%s, 'plan', %s, %s, "
                "'expense.added', 'expense', %s, '{}', now())",
                (str(new_id()), plan["id"], plan["id"], str(new_id())),
            ),
        ):
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                connection.transaction(),
            ):
                connection.execute(
                    "SELECT set_config('app.actor_id', %s, true)", (outsider.user_id,)
                )
                connection.execute(statement, params)


async def test_account_events_land_in_the_persons_own_scope(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    existing = await sign_in(api, identity_provider, subject="existing-sub", name="An")
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Pho", "base_currency": "VND"},
            headers=owner.headers,
        )
    ).json()

    async def guest_session() -> SignedIn:
        invite = await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
        redeemed = await api.post(
            "/v1/invites/redeem",
            json={"token": invite.json()["token"], "display_name": "Gia"},
        )
        assert redeemed.status_code == 200, redeemed.text
        return signed_in_from(redeemed.json()["session"])

    upgrading = await guest_session()
    upgraded = await api.post(
        "/v1/auth/google",
        json={"id_token": identity_provider.id_token(subject="new-sub")},
        headers=upgrading.headers,
    )
    assert upgraded.status_code == 200, upgraded.text
    own = await feed(api, upgrading, f"user:{upgrading.user_id}")
    assert [event["type"] for event in own] == ["account.guest_upgraded"]

    merging = await guest_session()
    claimed = await api.post(
        "/v1/auth/google",
        json={"id_token": identity_provider.id_token(subject="existing-sub")},
        headers=merging.headers,
    )
    assert claimed.status_code == 200, claimed.text
    account = signed_in_from(claimed.json())
    assert account.user_id == existing.user_id
    mine = await feed(api, account, f"user:{existing.user_id}")
    assert [(event["type"], event["summary"]) for event in mine] == [
        ("account.guest_merged", {"guest_user_id": merging.user_id})
    ]
    plan_feed = await feed(api, owner, f"plan:{plan['id']}")
    assert [event["type"] for event in plan_feed][-1] == "guest.linked"


async def test_every_release_one_event_type_is_written(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
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
    await ok(await api.post(trip.path("/leave"), headers=trip.members["Bea"].headers))

    types = {event["type"] for event in await feed(api, owner, f"plan:{trip.plan_id}")}
    assert types >= {
        "expense.voided",
        "expense.refunded",
        "waiver.given",
        "payment.recorded",
        "payment.reversed",
        "kitty.contributed",
        "kitty.withdrawn",
        "kitty.counted",
        "budget.changed",
        "ledger.consolidated",
        "ledger.consolidation_reversed",
        "plan.dates_changed",
        "base_currency.changed",
        "member.capabilities_changed",
        "member.role_changed",
        "member.left",
    }
