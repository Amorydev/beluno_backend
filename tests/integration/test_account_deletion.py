"""Deleting an account: "Former member" everywhere, money history intact."""

from __future__ import annotations

import asyncio
import time

import httpx
import psycopg
import pytest

from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import IntegrationEnvironment
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


async def new_plan(api: httpx.AsyncClient, owner: SignedIn, title: str) -> dict[str, str]:
    created = await api.post(
        "/v1/plans",
        json={"type": "hangout", "title": title, "base_currency": "VND"},
        headers=owner.headers,
    )
    assert created.status_code == 201, created.text
    plan: dict[str, str] = created.json()
    return plan


async def invite_token(api: httpx.AsyncClient, owner: SignedIn, plan_id: str) -> str:
    invite = await api.post(f"/v1/plans/{plan_id}/invites", json={}, headers=owner.headers)
    assert invite.status_code == 201, invite.text
    token: str = invite.json()["token"]
    return token


async def wait_for_lock_waiters(dsn: str, count: int) -> None:
    deadline = time.monotonic() + 10
    with psycopg.connect(dsn, autocommit=True) as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
            if waiting is not None and waiting[0] >= count:
                return
            await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} requests waiting on a lock")


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


async def test_guests_merged_into_the_account_lose_their_name_too(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    account = await sign_in(api, identity_provider, subject="an-sub", name="An")
    plan = await new_plan(api, owner, "Hot pot")
    await join_with_invite(api, owner, plan["id"], account)
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": await invite_token(api, owner, plan["id"]), "display_name": "An (phone)"},
    )
    assert joined.status_code == 200, joined.text
    guest = signed_in_from(joined.json()["session"])
    merged = await api.post(
        "/v1/auth/google",
        json={
            "id_token": identity_provider.id_token(subject="an-sub"),
            "merge_guest_participations": True,
        },
        headers=guest.headers,
    )
    assert merged.status_code == 200, merged.text
    _, cursor, _ = await pull_all(api, owner, f"plan:{plan['id']}")

    assert (await delete_me(api, signed_in_from(merged.json()))).status_code == 204

    assert sorted(
        admin.fetch(
            "SELECT display_name, access_state FROM plans.plan_participants WHERE plan_id = %s",
            plan["id"],
        )
    ) == [("Former member", "left"), ("Former member", "merged"), ("Linh", "active")]
    assert admin.fetch(
        "SELECT display_name, status FROM iam.users WHERE id = %s", guest.user_id
    ) == [("Former member", "deleted")]
    items, _, _ = await pull_all(api, owner, f"plan:{plan['id']}", cursor)
    renamed = {
        item["entity_id"]: item["data"]["display_name"]
        for item in items
        if item["entity_type"] == "plan_participant"
    }
    assert renamed[joined.json()["participant"]["id"]] == "Former member"


async def test_nobody_joins_a_plan_while_its_only_owner_deletes_the_account(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    environment: IntegrationEnvironment,
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    late = await sign_in(api, identity_provider, name="Late")
    plan = await new_plan(api, owner, "Night market")
    token = await invite_token(api, owner, plan["id"])

    # Hold the link so both requests queue on it: the deletion first, then the join.
    with psycopg.connect(environment.admin_dsn) as holder:
        holder.execute(
            "SELECT 1 FROM plans.plan_invites WHERE plan_id = %s FOR UPDATE", (plan["id"],)
        )
        deleting = asyncio.create_task(delete_me(api, owner))
        await wait_for_lock_waiters(environment.admin_dsn, 1)
        joining = asyncio.create_task(
            api.post("/v1/invites/redeem", json={"token": token}, headers=late.headers)
        )
        await wait_for_lock_waiters(environment.admin_dsn, 2)
        holder.commit()
        deleted, joined = await asyncio.gather(deleting, joining)

    assert deleted.status_code == 204, deleted.text
    assert joined.status_code == 404 and joined.json()["code"] == "INVITE_UNAVAILABLE"
    assert admin.fetch("SELECT state FROM plans.plan_invites WHERE plan_id = %s", plan["id"]) == [
        ("revoked",)
    ]
    assert admin.fetch(
        "SELECT display_name, role FROM plans.plan_participants WHERE plan_id = %s",
        plan["id"],
    ) == [("Former member", "owner")]


async def test_placeholders_never_hold_an_owner_back_but_guests_do(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    with_names = await new_plan(api, owner, "Family dinner")
    added = await api.post(
        f"/v1/plans/{with_names['id']}/participants",
        json={"placeholder_name": "Mom"},
        headers=owner.headers,
    )
    assert added.status_code == 201, added.text
    with_guest = await new_plan(api, owner, "Beach day")
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": await invite_token(api, owner, with_guest["id"]), "display_name": "Gia"},
    )
    assert joined.status_code == 200, joined.text

    refused = await delete_me(api, owner)
    assert refused.status_code == 409 and refused.json()["code"] == "OWNER_TRANSFER_REQUIRED"
    assert "Guests" in refused.json()["detail"]

    removed = await api.delete(
        f"/v1/plans/{with_guest['id']}/participants/{joined.json()['participant']['id']}",
        headers=owner.headers,
    )
    assert removed.status_code == 204
    assert (await delete_me(api, owner)).status_code == 204
    assert admin.scalar(
        "SELECT deletion_scheduled_at IS NOT NULL FROM plans.plans WHERE id = %s",
        with_names["id"],
    )


async def test_guests_delete_their_account_without_signing_in_again(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    plan = await new_plan(api, owner, "Coffee")
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": await invite_token(api, owner, plan["id"]), "display_name": "Gia"},
    )
    guest = signed_in_from(joined.json()["session"])
    admin.execute(
        "UPDATE iam.sessions SET authenticated_at = now() - interval '1 day' WHERE id = %s",
        guest.session_id,
    )

    assert (await delete_me(api, guest)).status_code == 204
    assert admin.fetch(
        "SELECT display_name, access_state FROM plans.plan_participants WHERE id = %s",
        joined.json()["participant"]["id"],
    ) == [("Former member", "left")]
