"""User-scope access signals and participant revival in the sync feed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.finance import join_with_invite
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


async def make_plan(api: httpx.AsyncClient, owner: SignedIn) -> dict[str, Any]:
    response = await api.post(
        "/v1/plans", json={"title": "Dinner", "base_currency": "USD"}, headers=owner.headers
    )
    assert response.status_code == 201, response.text
    plan: dict[str, Any] = response.json()
    return plan


async def test_user_scope_signals_plan_access_changes(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    plan = await make_plan(api, owner)
    joined = await join_with_invite(api, owner, plan["id"], member)
    scope = f"user:{member.user_id}"

    snapshot, cursor = await drain(api, member, scope, None)
    assert [item["entity_type"] for item in snapshot] == ["user", "session", "plan_access"]
    assert snapshot[2]["data"]["plan_id"] == plan["id"]
    assert snapshot[2]["data"]["access_state"] == "active"
    assert snapshot[2]["data"]["role"] == "member"

    promoted = await api.patch(
        f"/v1/plans/{plan['id']}/participants/{joined['id']}",
        json={"role": "admin"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert promoted.status_code == 200
    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{joined['id']}", headers=owner.headers
    )
    assert removed.status_code == 204

    changes, _ = await drain(api, member, scope, cursor)
    assert [(item["entity_type"], item["operation"]) for item in changes] == [
        ("plan_access", "upsert")
    ]
    assert changes[0]["data"]["access_state"] == "removed"
    assert changes[0]["data"]["role"] == "admin"
    assert changes[0]["data"]["version"] == 3


async def test_a_participant_who_leaves_and_rejoins_keeps_one_entity_with_newer_versions(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    plan = await make_plan(api, owner)
    joined = await join_with_invite(api, owner, plan["id"], member)
    scope = f"plan:{plan['id']}"
    _, cursor = await drain(api, owner, scope, None)

    left = await api.post(f"/v1/plans/{plan['id']}/leave", headers=member.headers)
    assert left.status_code == 204
    gone, cursor = await drain(api, owner, scope, cursor)
    assert [(item["entity_id"], item["version"]) for item in gone] == [(joined["id"], 2)]
    assert gone[0]["data"]["access_state"] == "left"

    rejoined = await join_with_invite(api, owner, plan["id"], member)
    assert rejoined["id"] == joined["id"]
    back, _ = await drain(api, owner, scope, cursor)
    rows = [item for item in back if item["entity_type"] == "plan_participant"]
    assert [(item["entity_id"], item["operation"], item["version"]) for item in rows] == [
        (joined["id"], "upsert", 3)
    ]
    assert rows[0]["data"]["access_state"] == "active"
