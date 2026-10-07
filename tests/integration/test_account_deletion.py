"""Deleting an account: "Former member" everywhere, money history intact."""

from __future__ import annotations

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    add_expense,
    equal_expense,
    finance_plan,
    join_with_invite,
    ledger_balances,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def delete_me(api: httpx.AsyncClient, user: SignedIn) -> httpx.Response:
    return await api.delete("/v1/me", headers=user.headers)


async def test_owners_transfer_first_and_deletion_needs_a_recent_sign_in(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    friend = await sign_in(api, identity_provider, name="Minh")
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "trip", "title": "Da Lat", "base_currency": "VND"},
            headers=owner.headers,
        )
    ).json()
    await join_with_invite(api, owner, plan["id"], friend)

    refused = await delete_me(api, owner)
    assert refused.status_code == 409 and refused.json()["code"] == "OWNER_TRANSFER_REQUIRED"
    assert admin.scalar("SELECT status FROM iam.users WHERE id = %s", owner.user_id) == "active"

    admin.execute(
        "UPDATE iam.sessions SET authenticated_at = now() - interval '1 day' WHERE id = %s",
        friend.session_id,
    )
    stale = await delete_me(api, friend)
    assert stale.status_code == 403 and stale.json()["code"] == "STEP_UP_REQUIRED"


async def test_a_deleted_member_becomes_former_member_and_money_still_settles(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    ann, bea, dan = (trip.people[name] for name in ("Ann", "Bea", "Dan"))
    bea_user = trip.members["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(900, ann, [ann, bea, dan]))
    balances = await ledger_balances(api, trip.owner, trip)
    solo = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Solo walk", "base_currency": "USD"},
            headers=bea_user.headers,
        )
    ).json()
    own_crew = await api.post(
        "/v1/crews",
        json={"name": "Mine", "member_user_ids": [trip.owner.user_id]},
        headers=bea_user.headers,
    )
    assert own_crew.status_code == 201, own_crew.text
    shared_crew = await api.post(
        "/v1/crews",
        json={
            "name": "Trip crew",
            "member_user_ids": [bea_user.user_id, trip.members["Dan"].user_id],
        },
        headers=trip.owner.headers,
    )
    only_bea = await api.post(
        "/v1/crews",
        json={"name": "Just Bea", "member_user_ids": [bea_user.user_id]},
        headers=trip.owner.headers,
    )
    assert shared_crew.status_code == only_bea.status_code == 201
    _, owner_cursor, _ = await pull_all(api, trip.owner, f"user:{trip.owner.user_id}")

    deleted = await delete_me(api, bea_user)
    assert deleted.status_code == 204, deleted.text

    # The account is gone: the session no longer works, the profile holds nothing.
    assert (await api.get("/v1/me", headers=bea_user.headers)).status_code == 401
    assert admin.fetch(
        "SELECT status, email, display_name, locale, default_currency FROM iam.users WHERE id = %s",
        bea_user.user_id,
    ) == [("deleted", None, "Former member", None, None)]
    assert (
        admin.scalar(
            "SELECT count(*) FROM iam.user_identities WHERE user_id = %s", bea_user.user_id
        )
        == 0
    )

    # In the trip: "Former member", left, and every balance unchanged and settleable.
    assert admin.fetch(
        "SELECT display_name, access_state FROM plans.plan_participants WHERE id = %s", bea
    ) == [("Former member", "left")]
    assert await ledger_balances(api, trip.owner, trip) == balances
    paid = await api.post(
        trip.path("/settlements"),
        json={
            "from_participant_id": bea,
            "to_participant_id": ann,
            "currency": "USD",
            "amount_minor": 300,
            "occurred_on": "2026-10-07",
        },
        headers=trip.owner.headers,
    )
    assert paid.status_code == 201, paid.text
    assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", trip.plan_id) == []
    synced, _, _ = await pull_all(api, trip.owner, f"plan:{trip.plan_id}")
    feed = [item["data"] for item in synced if item["entity_type"] == "activity_event"]
    [row] = [item["data"] for item in synced if item["entity_id"] == bea]
    assert (row["display_name"], row["access_state"]) == ("Former member", "left")
    assert any(
        event["type"] == "member.left" and event["actor_user_id"] == bea_user.user_id
        for event in feed
    )

    # The plan Bea owned alone is scheduled for deletion; her crews are gone.
    assert admin.scalar(
        "SELECT deletion_scheduled_at IS NOT NULL FROM plans.plans WHERE id = %s", solo["id"]
    )
    assert admin.scalar(
        "SELECT deleted_at IS NOT NULL FROM people.crews WHERE id = %s", own_crew.json()["id"]
    )
    # Others' crews drop her; one left with nobody is deleted, and their devices hear of it.
    crews = {
        row["id"]: row for row in (await api.get("/v1/crews", headers=trip.owner.headers)).json()
    }
    assert [m["user_id"] for m in crews[shared_crew.json()["id"]]["members"]] == [
        trip.members["Dan"].user_id
    ]
    assert only_bea.json()["id"] not in crews
    items, _, _ = await pull_all(api, trip.owner, f"user:{trip.owner.user_id}", owner_cursor)
    crew_changes = {
        item["entity_id"]: item["operation"] for item in items if item["entity_type"] == "crew"
    }
    assert crew_changes == {
        shared_crew.json()["id"]: "upsert",
        only_bea.json()["id"]: "delete",
    }


async def test_signing_in_again_starts_a_new_account(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    gone = await sign_in(api, identity_provider, name="Gia", subject="gia-sub")
    assert (await delete_me(api, gone)).status_code == 204
    again = await sign_in(api, identity_provider, name="Gia", subject="gia-sub")
    assert again.user_id != gone.user_id
    assert again.profile["display_name"] == "Gia"
    assert admin.scalar("SELECT status FROM iam.users WHERE id = %s", gone.user_id) == "deleted"
