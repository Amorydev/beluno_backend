"""Group lifecycle, membership, and ownership on real PostgreSQL with RLS."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

GROUP = {"name": "Thursday Crew", "default_currency": "VND", "default_timezone": "Asia/Ho_Chi_Minh"}


async def create_group(api: httpx.AsyncClient, owner: SignedIn, **overrides: str) -> dict:
    response = await api.post("/v1/groups", json={**GROUP, **overrides}, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


def seed_member(admin: AdminDatabase, group_id: str, user_id: str, role: str = "member") -> None:
    admin.execute(
        "INSERT INTO groups.group_memberships (group_id, user_id, role, state, joined_at, version, "
        "created_at, updated_at) VALUES (%s, %s, %s, 'active', now(), 1, now(), now())",
        group_id,
        user_id,
        role,
    )


async def test_owner_creates_updates_and_lists_a_group(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    group = await create_group(api, owner)
    assert group["my_role"] == "owner"
    assert group["version"] == 1

    updated = await api.patch(
        f"/v1/groups/{group['id']}",
        json={"name": "Friday Crew", "default_currency": "USD"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Friday Crew"
    assert updated.headers["etag"] == '"2"'
    stale = await api.patch(
        f"/v1/groups/{group['id']}",
        json={"name": "Old"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert stale.status_code == 412

    listed = await api.get("/v1/groups", headers=owner.headers)
    assert [item["id"] for item in listed.json()["items"]] == [group["id"]]
    audit = admin.fetch(
        "SELECT action FROM sync_audit.audit_events WHERE group_id = %s "
        "ORDER BY occurred_at, action",
        group["id"],
    )
    assert ("group.created",) in audit and ("group.updated",) in audit
    changes = admin.scalar(
        "SELECT count(*) FROM sync_audit.change_log WHERE scope_type = 'group' AND scope_id = %s",
        group["id"],
    )
    assert changes == 3


async def test_client_generated_group_id_is_respected_and_unique(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    group_id = str(uuid4())
    created = await create_group(api, owner, id=group_id)
    assert created["id"] == group_id
    duplicate = await api.post("/v1/groups", json={**GROUP, "id": group_id}, headers=owner.headers)
    assert duplicate.status_code == 409


async def test_outsiders_cannot_see_or_probe_a_group(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    outsider = await sign_in(api, identity_provider)
    group = await create_group(api, owner)

    for path in ("", "/members"):
        response = await api.get(f"/v1/groups/{group['id']}{path}", headers=outsider.headers)
        assert response.status_code == 404
    missing = await api.get(f"/v1/groups/{uuid4()}", headers=outsider.headers)
    assert missing.json()["code"] == response.json()["code"] == "NOT_FOUND"
    patch = await api.patch(
        f"/v1/groups/{group['id']}",
        json={"name": "x"},
        headers={**outsider.headers, "If-Match": '"1"'},
    )
    assert patch.status_code == 404
    # The owner cannot add a stranger they share nothing with.
    add = await api.post(
        f"/v1/groups/{group['id']}/members",
        json={"user_id": outsider.user_id},
        headers=owner.headers,
    )
    assert add.status_code == 404


async def test_membership_invitation_role_and_removal_lifecycle(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider, name="Friend")
    shared = await create_group(api, owner, name="Shared")
    seed_member(admin, shared["id"], friend.user_id)
    group = await create_group(api, owner)

    invited = await api.post(
        f"/v1/groups/{group['id']}/members", json={"user_id": friend.user_id}, headers=owner.headers
    )
    assert invited.status_code == 201
    assert invited.json()["state"] == "invited"
    assert invited.json()["display_name"] == "Friend"
    # Invitees see the group but not its members until they accept.
    assert (await api.get(f"/v1/groups/{group['id']}", headers=friend.headers)).status_code == 200
    members = await api.get(f"/v1/groups/{group['id']}/members", headers=friend.headers)
    assert members.status_code == 403
    accepted = await api.post(
        f"/v1/groups/{group['id']}/invitation", json={"accept": True}, headers=friend.headers
    )
    assert accepted.json()["my_membership_state"] == "active"

    promoted = await api.patch(
        f"/v1/groups/{group['id']}/members/{friend.user_id}",
        json={"role": "admin"},
        headers={**owner.headers, "If-Match": '"2"'},
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"
    # Admins cannot remove the owner.
    blocked = await api.delete(
        f"/v1/groups/{group['id']}/members/{owner.user_id}", headers=friend.headers
    )
    assert blocked.status_code == 403
    removed = await api.delete(
        f"/v1/groups/{group['id']}/members/{friend.user_id}", headers=owner.headers
    )
    assert removed.status_code == 204
    assert (await api.get(f"/v1/groups/{group['id']}", headers=friend.headers)).status_code == 404


async def test_last_owner_cannot_leave_and_transfer_requires_step_up(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    member = await sign_in(api, identity_provider)
    group = await create_group(api, owner)
    seed_member(admin, group["id"], member.user_id)

    leave = await api.delete(
        f"/v1/groups/{group['id']}/members/{owner.user_id}", headers=owner.headers
    )
    assert leave.status_code == 409
    assert leave.json()["code"] == "OWNER_TRANSFER_REQUIRED"

    admin.execute("UPDATE iam.sessions SET authenticated_at = now() - interval '1 hour'")
    stale_auth = await api.post(
        f"/v1/groups/{group['id']}/ownership-transfer",
        json={"new_owner_user_id": member.user_id},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert stale_auth.status_code == 403
    assert stale_auth.json()["code"] == "STEP_UP_REQUIRED"

    admin.execute("UPDATE iam.sessions SET authenticated_at = now()")
    transferred = await api.post(
        f"/v1/groups/{group['id']}/ownership-transfer",
        json={"new_owner_user_id": member.user_id},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert transferred.status_code == 200
    assert transferred.json()["my_role"] == "admin"
    owners = admin.fetch(
        "SELECT user_id::text FROM groups.group_memberships WHERE group_id = %s AND role = 'owner'",
        group["id"],
    )
    assert owners == [(member.user_id,)]
    left = await api.delete(
        f"/v1/groups/{group['id']}/members/{owner.user_id}", headers=owner.headers
    )
    assert left.status_code == 204


async def test_concurrent_ownership_transfers_have_one_winner(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    first = await sign_in(api, identity_provider)
    second = await sign_in(api, identity_provider)
    group = await create_group(api, owner)
    seed_member(admin, group["id"], first.user_id)
    seed_member(admin, group["id"], second.user_id)

    responses = await asyncio.gather(
        *(
            api.post(
                f"/v1/groups/{group['id']}/ownership-transfer",
                json={"new_owner_user_id": target.user_id},
                headers={**owner.headers, "If-Match": '"1"'},
            )
            for target in (first, second)
        )
    )

    statuses = sorted(response.status_code for response in responses)
    # The loser is serialized behind the winner's row lock and is no longer the owner.
    assert statuses[0] == 200 and statuses[1] in (403, 412)
    assert (
        admin.scalar(
            "SELECT count(*) FROM groups.group_memberships WHERE group_id = %s AND role = 'owner'",
            group["id"],
        )
        == 1
    )


async def test_group_deletion_is_scheduled_and_restorable(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    group = await create_group(api, owner)

    scheduled = await api.delete(
        f"/v1/groups/{group['id']}", headers={**owner.headers, "If-Match": '"1"'}
    )
    assert scheduled.status_code == 200
    assert scheduled.json()["state"] == "deletion_scheduled"
    frozen = await api.patch(
        f"/v1/groups/{group['id']}",
        json={"name": "x"},
        headers={**owner.headers, "If-Match": '"2"'},
    )
    assert frozen.status_code == 403
    restored = await api.post(
        f"/v1/groups/{group['id']}/restore", headers={**owner.headers, "If-Match": '"2"'}
    )
    assert restored.json()["state"] == "active"
