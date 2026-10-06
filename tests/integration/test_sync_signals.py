"""User-scope access signals and member-name refresh in the sync feed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def drain(
    api: httpx.AsyncClient, user: SignedIn, scope: str, cursor: str | None
) -> tuple[list[dict[str, Any]], str]:
    items: list[dict[str, Any]] = []
    while True:
        response = await api.post(
            "/v1/sync/pull",
            json={"scopes": [{"scope": scope, "cursor": cursor}]},
            headers=user.headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()["scopes"][0]
        assert page["status"] == "ok", page
        items.extend(page["changes"])
        cursor = page["cursor"]
        if not page["has_more"]:
            return items, cursor


async def test_user_scope_signals_plan_and_group_access_changes(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    group = (
        await api.post(
            "/v1/groups",
            json={"name": "Crew", "default_currency": "USD", "default_timezone": "UTC"},
            headers=owner.headers,
        )
    ).json()
    link = await api.post(f"/v1/groups/{group['id']}/invites", json={}, headers=owner.headers)
    joined = await api.post(
        "/v1/invites/redeem", json={"token": link.json()["token"]}, headers=member.headers
    )
    assert joined.status_code == 200
    plan = (
        await api.post(
            "/v1/plans",
            json={
                "title": "Dinner",
                "group_id": group["id"],
                "participants": [{"user_id": member.user_id}],
            },
            headers=owner.headers,
        )
    ).json()
    scope = f"user:{member.user_id}"

    snapshot, cursor = await drain(api, member, scope, None)
    assert [item["entity_type"] for item in snapshot] == [
        "user",
        "session",
        "group_access",
        "plan_access",
    ]
    assert snapshot[2]["data"] == {
        "group_id": group["id"],
        "role": "member",
        "state": "active",
        "version": 1,
    }
    assert snapshot[3]["data"]["plan_id"] == plan["id"]
    assert snapshot[3]["data"]["access_state"] == "active"
    assert snapshot[3]["data"]["role"] == "member"

    participants = (
        await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)
    ).json()
    participant_id = next(p["id"] for p in participants if p["user_id"] == member.user_id)
    promoted = await api.patch(
        f"/v1/plans/{plan['id']}/participants/{participant_id}",
        json={"role": "admin"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert promoted.status_code == 200
    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{participant_id}", headers=owner.headers
    )
    assert removed.status_code == 204
    demoted = await api.delete(
        f"/v1/groups/{group['id']}/members/{member.user_id}", headers=owner.headers
    )
    assert demoted.status_code == 204

    changes, _ = await drain(api, member, scope, cursor)
    assert [(item["entity_type"], item["operation"]) for item in changes] == [
        ("plan_access", "upsert"),
        ("group_access", "upsert"),
    ]
    assert changes[0]["data"]["access_state"] == "removed"
    assert changes[0]["data"]["role"] == "admin"
    assert changes[0]["data"]["version"] == 3
    assert changes[1]["data"]["state"] == "removed"
    assert changes[1]["data"]["version"] == 2


async def test_renaming_yourself_refreshes_member_rows_in_your_groups(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    group = (
        await api.post(
            "/v1/groups",
            json={"name": "Crew", "default_currency": "USD", "default_timezone": "UTC"},
            headers=owner.headers,
        )
    ).json()
    link = await api.post(f"/v1/groups/{group['id']}/invites", json={}, headers=owner.headers)
    await api.post(
        "/v1/invites/redeem", json={"token": link.json()["token"]}, headers=member.headers
    )
    scope = f"group:{group['id']}"
    _, cursor = await drain(api, owner, scope, None)

    renamed = await api.patch(
        "/v1/me",
        json={"display_name": "Renamed Member"},
        headers={**member.headers, "If-Match": '"1"'},
    )
    assert renamed.status_code == 200
    locale_only = await api.patch(
        "/v1/me", json={"locale": "vi-VN"}, headers={**member.headers, "If-Match": '"2"'}
    )
    assert locale_only.status_code == 200

    changes, _ = await drain(api, owner, scope, cursor)
    assert [(item["entity_type"], item["entity_id"], item["operation"]) for item in changes] == [
        ("group_membership", member.user_id, "upsert")
    ]
    assert changes[0]["data"]["display_name"] == "Renamed Member"
    assert changes[0]["version"] == 1


async def test_a_member_who_leaves_and_rejoins_is_revived_by_a_newer_version(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    group = (
        await api.post(
            "/v1/groups",
            json={"name": "Crew", "default_currency": "USD", "default_timezone": "UTC"},
            headers=owner.headers,
        )
    ).json()
    link = await api.post(f"/v1/groups/{group['id']}/invites", json={}, headers=owner.headers)
    token = link.json()["token"]
    assert (
        await api.post("/v1/invites/redeem", json={"token": token}, headers=member.headers)
    ).status_code == 200
    scope = f"group:{group['id']}"
    _, cursor = await drain(api, owner, scope, None)

    left = await api.delete(
        f"/v1/groups/{group['id']}/members/{member.user_id}", headers=member.headers
    )
    assert left.status_code == 204
    gone, cursor = await drain(api, owner, scope, cursor)
    assert [(item["operation"], item["version"]) for item in gone] == [("delete", 2)]

    rejoined = await api.post("/v1/invites/redeem", json={"token": token}, headers=member.headers)
    assert rejoined.status_code == 200
    back, _ = await drain(api, owner, scope, cursor)
    memberships = [item for item in back if item["entity_type"] == "group_membership"]
    # Same entity ID, higher version: the tombstone (2) must not suppress this upsert (3).
    assert [(item["entity_id"], item["operation"], item["version"]) for item in memberships] == [
        (member.user_id, "upsert", 3)
    ]
    assert memberships[0]["data"]["state"] == "active"
