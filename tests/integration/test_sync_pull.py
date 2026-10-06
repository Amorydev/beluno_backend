"""Handshake directory and cursor-based pull: snapshots, change pages, and cursor safety."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest

from beluno.config import Settings
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import join_with_invite
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: Any) -> dict[str, Any]:
    payload = {"title": "Dinner", "base_currency": "USD", **body}
    response = await api.post("/v1/plans", json=payload, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def handshake(api: httpx.AsyncClient, user: SignedIn, **body: Any) -> dict[str, Any]:
    response = await api.post(
        "/v1/sync/handshake", json={"protocol_version": 1, **body}, headers=user.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def pull_once(
    api: httpx.AsyncClient,
    user: SignedIn,
    scope: str,
    cursor: str | None,
    *,
    page_size: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"scopes": [{"scope": scope, "cursor": cursor}]}
    if page_size is not None:
        body["page_size"] = page_size
    response = await api.post("/v1/sync/pull", json=body, headers=user.headers)
    assert response.status_code == 200, response.text
    return response.json()["scopes"][0]


async def drain(
    api: httpx.AsyncClient,
    user: SignedIn,
    scope: str,
    cursor: str | None,
    *,
    page_size: int | None = None,
) -> tuple[list[dict[str, Any]], str, int]:
    """Pull until ``has_more`` is false; returns items, the final cursor, and the page count."""

    items: list[dict[str, Any]] = []
    pages = 0
    while True:
        page = await pull_once(api, user, scope, cursor, page_size=page_size)
        assert page["status"] == "ok", page
        items.extend(page["changes"])
        cursor = page["cursor"]
        pages += 1
        if not page["has_more"]:
            return items, cursor, pages


def entities(items: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {(item["entity_type"], item["entity_id"], item["operation"]) for item in items}


async def test_handshake_lists_scopes_by_current_access(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    outsider = await sign_in(api, identity_provider, name="Outsider")
    shared = await make_plan(api, owner)
    private = await make_plan(api, owner)
    await join_with_invite(api, owner, shared["id"], member)

    for version in (2, 99):
        outdated = await api.post(
            "/v1/sync/handshake", json={"protocol_version": version}, headers=owner.headers
        )
        assert outdated.status_code == 426
        assert outdated.json()["code"] == "CLIENT_UPGRADE_REQUIRED"

    owner_view = await handshake(api, owner)
    assert owner_view["protocol"] == {"version": 1, "min_version": 1, "max_version": 1}
    assert owner_view["limits"]["push_max_operations"] == 100
    assert owner_view["retention"]["offline_window_days"] == 90
    assert {command["name"] for command in owner_view["commands"]} >= {"plan.update", "plan.rsvp"}
    assert owner_view["next_directory_cursor"] is None
    owner_scopes = {entry["scope"]: entry["access"] for entry in owner_view["scopes"]}
    assert owner_scopes == {
        f"user:{owner.user_id}": "self",
        f"plan:{shared['id']}": "manager",
        f"plan:{private['id']}": "manager",
    }
    assert owner_view["scopes"][0]["scope"] == f"user:{owner.user_id}"
    heads = {entry["scope"]: entry["head"] for entry in owner_view["scopes"]}
    assert heads[f"plan:{private['id']}"] == 2 and heads[f"user:{owner.user_id}"] >= 2

    member_scopes = {
        entry["scope"]: entry["access"] for entry in (await handshake(api, member))["scopes"]
    }
    assert member_scopes == {
        f"user:{member.user_id}": "self",
        f"plan:{shared['id']}": "member",
    }
    outsider_scopes = [entry["scope"] for entry in (await handshake(api, outsider))["scopes"]]
    assert outsider_scopes == [f"user:{outsider.user_id}"]

    # Leaving the plan drops its scope from the directory.
    left = await api.post(f"/v1/plans/{shared['id']}/leave", headers=member.headers)
    assert left.status_code == 204
    after_leaving = {e["scope"] for e in (await handshake(api, member))["scopes"]}
    assert after_leaving == {f"user:{member.user_id}"}


async def test_bootstrap_then_changes_converge_on_a_plan(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner, participants=[{"placeholder_name": "Grandma"}])
    budget = await api.post(
        f"/v1/plans/{plan['id']}/budgets",
        json={"scope": "total", "limit_minor": 50_000},
        headers=owner.headers,
    )
    assert budget.status_code == 201, budget.text
    invite = await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    scope = f"plan:{plan['id']}"

    snapshot, cursor, pages = await drain(api, owner, scope, None)
    assert pages == 1
    assert all(item["seq"] is None and item["operation"] == "upsert" for item in snapshot)
    assert [item["entity_type"] for item in snapshot] == [
        "plan",
        "plan_participant",
        "plan_participant",
        "plan_invite",
        "ledger",
        "budget",
    ]
    plan_item = snapshot[0]
    assert plan_item["data"]["title"] == "Dinner" and plan_item["version"] == 1
    # Entities never embed other entities: no my_participant here.
    assert "my_participant" not in plan_item["data"]
    assert (
        snapshot[1]["data"]["role"] == "owner" and snapshot[1]["data"]["user_id"] == owner.user_id
    )
    invite_item = snapshot[3]
    assert invite_item["entity_id"] == invite.json()["id"]
    assert "token" not in invite_item["data"]
    assert snapshot[5]["entity_id"] == budget.json()["id"]

    # Nothing new: an empty page with the same position.
    quiet = await pull_once(api, owner, scope, cursor)
    assert quiet["changes"] == [] and quiet["has_more"] is False and quiet["head"] == 6

    renamed = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "Lunch"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert renamed.status_code == 200
    again = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "Brunch"},
        headers={**owner.headers, "If-Match": '"2"'},
    )
    assert again.status_code == 200
    deleted = await api.delete(
        f"/v1/plans/{plan['id']}/budgets/{budget.json()['id']}", headers=owner.headers
    )
    assert deleted.status_code == 204

    changes, cursor, _ = await drain(api, owner, scope, quiet["cursor"])
    # Two plan edits collapse into one item carrying the latest state; the delete is a tombstone.
    assert [(item["entity_type"], item["operation"], item["seq"]) for item in changes] == [
        ("plan", "upsert", 8),
        ("budget", "delete", 9),
    ]
    assert changes[0]["data"]["title"] == "Brunch" and changes[0]["version"] == 3
    assert changes[1]["data"] is None and changes[1]["version"] == 2
    assert all(item["changed_at"] is not None for item in changes)

    # Replaying the previous cursor returns the same sequences again.
    replay = await pull_once(api, owner, scope, quiet["cursor"])
    assert [item["seq"] for item in replay["changes"]] == [8, 9]
    assert replay["cursor"] == cursor


async def test_pages_stop_at_the_watermark_and_snapshot_pages_cover_everything(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(
        api, owner, participants=[{"placeholder_name": f"Guest {index}"} for index in range(12)]
    )
    scope = f"plan:{plan['id']}"

    snapshot, cursor, pages = await drain(api, owner, scope, None, page_size=10)
    assert pages == 2
    assert len(snapshot) == 14  # plan + owner + 12 placeholders
    assert len({item["entity_id"] for item in snapshot}) == 14

    # 12 RSVP/role changes to pull; interleave a new change while paging.
    people = [item for item in snapshot if item["entity_type"] == "plan_participant"]
    placeholders = [item for item in people if item["data"]["identity_kind"] == "placeholder"]
    for item in placeholders:
        response = await api.patch(
            f"/v1/plans/{plan['id']}/participants/{item['entity_id']}",
            json={"role": "viewer"},
            headers={**owner.headers, "If-Match": '"1"'},
        )
        assert response.status_code == 200, response.text
    first = await pull_once(api, owner, scope, cursor, page_size=10)
    assert first["has_more"] is True and len(first["changes"]) == 10
    assert first["head"] == 26
    late = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "Late"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert late.status_code == 200
    second = await pull_once(api, owner, scope, first["cursor"], page_size=10)
    assert second["has_more"] is False
    assert [item["seq"] for item in second["changes"]] == [25, 26]
    assert second["head"] == 27
    third = await pull_once(api, owner, scope, second["cursor"], page_size=10)
    assert [item["seq"] for item in third["changes"]] == [27]
    assert third["changes"][0]["data"]["title"] == "Late"


async def test_visibility_follows_role_and_revocation(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    outsider = await sign_in(api, identity_provider, name="Outsider")
    plan = await make_plan(api, owner)
    await join_with_invite(api, owner, plan["id"], member)
    invite = await api.post(
        f"/v1/plans/{plan['id']}/invites", json={"requires_approval": True}, headers=owner.headers
    )
    applicant = await sign_in(api, identity_provider, name="Applicant")
    applied = await api.post(
        "/v1/invites/redeem", json={"token": invite.json()["token"]}, headers=applicant.headers
    )
    assert applied.status_code == 200 and applied.json()["status"] == "pending_approval"
    scope = f"plan:{plan['id']}"

    manager_items, manager_cursor, _ = await drain(api, owner, scope, None)
    member_items, member_cursor, _ = await drain(api, member, scope, None)
    assert {item["entity_type"] for item in manager_items} == {
        "plan",
        "plan_participant",
        "plan_invite",
    }
    assert {item["entity_type"] for item in member_items} == {"plan", "plan_participant"}
    pending = {
        item["entity_id"]
        for item in manager_items
        if item["data"].get("access_state") == "pending_approval"
    }
    assert pending == {applied.json()["participant"]["id"]}
    assert not any(item["entity_id"] in pending for item in member_items)
    assert (await pull_once(api, outsider, scope, None))["status"] == "unavailable"
    assert (await pull_once(api, applicant, scope, None))["status"] == "unavailable"

    # The member is promoted: the old cursor was issued for a lower level.
    member_participant = next(
        item for item in member_items if item["data"].get("user_id") == member.user_id
    )
    promoted = await api.patch(
        f"/v1/plans/{plan['id']}/participants/{member_participant['entity_id']}",
        json={"role": "admin"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert promoted.status_code == 200
    assert (await pull_once(api, member, scope, member_cursor))["status"] == "resync_required"
    promoted_items, _, _ = await drain(api, member, scope, None)
    assert "plan_invite" in {item["entity_type"] for item in promoted_items}

    # Removal: the scope is gone for the removed person and a change for everyone else.
    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{member_participant['entity_id']}",
        headers=owner.headers,
    )
    assert removed.status_code == 204
    assert (await pull_once(api, member, scope, None))["status"] == "unavailable"
    owner_changes, _, _ = await drain(api, owner, scope, manager_cursor)
    removal = [
        item for item in owner_changes if item["entity_id"] == member_participant["entity_id"]
    ]
    assert removal[-1]["operation"] == "upsert" and removal[-1]["data"]["access_state"] == "removed"


async def test_group_scopes_are_no_longer_syncable(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    person = await sign_in(api, identity_provider, name="Person")
    response = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": f"group:{uuid4()}", "cursor": None}]},
        headers=person.headers,
    )
    assert response.status_code == 422


async def test_user_scope_carries_profile_and_sessions(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    person = await sign_in(api, identity_provider, name="Person", subject="person-1")
    scope = f"user:{person.user_id}"
    items, cursor, _ = await drain(api, person, scope, None)
    assert [item["entity_type"] for item in items] == ["user", "session"]
    assert items[1]["data"]["current"] is True

    other_device = await sign_in(api, identity_provider, name="Person", subject="person-1")
    assert other_device.user_id == person.user_id
    renamed = await api.patch(
        "/v1/me", json={"display_name": "Renamed"}, headers={**person.headers, "If-Match": '"1"'}
    )
    assert renamed.status_code == 200
    changes, cursor, _ = await drain(api, person, scope, cursor)
    assert [(item["entity_type"], item["operation"]) for item in changes] == [
        ("session", "upsert"),
        ("user", "upsert"),
    ]
    assert changes[0]["entity_id"] == other_device.session_id
    assert changes[0]["data"]["current"] is False
    assert changes[1]["data"]["display_name"] == "Renamed"

    logout = await api.post("/v1/auth/logout", headers=person.headers)
    assert logout.status_code == 204
    # The other device sees the sign-out as a tombstone; strangers see nothing.
    revoked, _, _ = await drain(api, other_device, scope, cursor)
    assert [(item["entity_type"], item["operation"], item["entity_id"]) for item in revoked] == [
        ("session", "delete", person.session_id)
    ]
    stranger = await sign_in(api, identity_provider, name="Stranger")
    assert (await pull_once(api, stranger, scope, None))["status"] == "unavailable"


async def test_cursor_safety_checks(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    other = await sign_in(api, identity_provider, name="Other")
    plan = await make_plan(api, owner)
    invite = await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    joined = await api.post(
        "/v1/invites/redeem", json={"token": invite.json()["token"]}, headers=other.headers
    )
    assert joined.status_code == 200, joined.text
    scope = f"plan:{plan['id']}"
    _, cursor, _ = await drain(api, owner, scope, None)
    _, other_cursor, _ = await drain(api, other, scope, None)

    tampered = cursor[:-3] + ("AAA" if not cursor.endswith("AAA") else "BBB")
    assert (await pull_once(api, owner, scope, tampered))["status"] == "resync_required"
    assert (await pull_once(api, owner, scope, other_cursor))["status"] == "resync_required"
    wrong_scope = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": f"user:{owner.user_id}", "cursor": cursor}]},
        headers=owner.headers,
    )
    assert wrong_scope.status_code == 422
    duplicated = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": scope, "cursor": None}, {"scope": scope, "cursor": None}]},
        headers=owner.headers,
    )
    assert duplicated.status_code == 422
    malformed = await api.post(
        "/v1/sync/pull", json={"scopes": [{"scope": "plan:not-a-uuid"}]}, headers=owner.headers
    )
    assert malformed.status_code == 422

    admin.execute(
        "UPDATE sync_audit.scope_heads SET generation = generation + 1 WHERE scope_id = %s",
        plan["id"],
    )
    assert (await pull_once(api, owner, scope, cursor))["status"] == "resync_required"
    _, fresh, _ = await drain(api, owner, scope, None)
    assert (await pull_once(api, owner, scope, fresh))["status"] == "ok"

    # A compaction floor above the cursor, and a head below it (restore), both force a resync.
    admin.execute(
        "UPDATE sync_audit.scope_heads SET floor_seq = last_seq WHERE scope_id = %s", plan["id"]
    )
    assert (await pull_once(api, owner, scope, fresh))["status"] == "ok"
    admin.execute(
        "UPDATE sync_audit.scope_heads SET floor_seq = last_seq + 1, last_seq = last_seq + 1 "
        "WHERE scope_id = %s",
        plan["id"],
    )
    assert (await pull_once(api, owner, scope, fresh))["status"] == "resync_required"
    admin.execute(
        "UPDATE sync_audit.scope_heads SET floor_seq = 0, last_seq = 1 WHERE scope_id = %s",
        plan["id"],
    )
    assert (await pull_once(api, owner, scope, fresh))["status"] == "resync_required"


async def test_pull_kill_switch(
    live_settings: Settings, identity_provider: IdentityProviderStub
) -> None:
    from beluno.api.main import create_app
    from beluno.db.session import Database

    settings = live_settings.model_copy(update={"sync_pull_enabled": False})
    database = Database(settings)
    app = create_app(
        settings=settings, database=database, identity_verifier=identity_provider.verifier(settings)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as api:
        person = await sign_in(api, identity_provider, name="Person")
        shake = await handshake(api, person)
        refused = await api.post(
            "/v1/sync/pull",
            json={"scopes": [{"scope": f"user:{person.user_id}"}]},
            headers=person.headers,
        )
    await database.close()
    assert shake["features"]["pull_enabled"] is False
    assert refused.status_code == 503 and refused.json()["code"] == "FEATURE_DISABLED"


async def test_directory_is_paginated(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beluno.sync import directory

    monkeypatch.setattr(directory, "DIRECTORY_PAGE_SIZE", 2)
    owner = await sign_in(api, identity_provider, name="Owner")
    for _ in range(3):
        await make_plan(api, owner)

    first = await handshake(api, owner)
    assert len(first["scopes"]) == 2 and first["next_directory_cursor"] is not None
    second = await handshake(api, owner, directory_cursor=first["next_directory_cursor"])
    assert len(second["scopes"]) == 2 and second["next_directory_cursor"] is None
    scopes = [entry["scope"] for entry in first["scopes"] + second["scopes"]]
    assert len(set(scopes)) == 4 and scopes[0] == f"user:{owner.user_id}"
    bad = await api.post(
        "/v1/sync/handshake",
        json={"protocol_version": 1, "directory_cursor": "###"},
        headers=owner.headers,
    )
    assert bad.status_code == 422


async def test_unknown_future_entity_types_are_skipped(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    live_settings: Settings,
) -> None:
    import json

    import psycopg

    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    scope = f"plan:{plan['id']}"
    _, cursor, _ = await drain(api, owner, scope, None)
    dsn = live_settings.api_database_dsn
    assert dsn is not None
    with (
        psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as connection,
        connection.transaction(),
    ):
        connection.execute(
            "SELECT sync_audit.append_changes(%s::jsonb)",
            [
                json.dumps(
                    [
                        {
                            "changed_at": "2026-10-06T00:00:00Z",
                            "scope_type": "plan",
                            "scope_id": plan["id"],
                            "entity_type": "poll",
                            "entity_id": str(uuid4()),
                            "entity_version": 1,
                            "operation": "upsert",
                        }
                    ]
                )
            ],
        )
    page = await pull_once(api, owner, scope, cursor)
    assert page["changes"] == [] and page["has_more"] is False and page["head"] == 3


async def test_the_maximum_page_size_never_skips_changes(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    live_settings: Settings,
) -> None:
    import json

    import psycopg

    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    scope = f"plan:{plan['id']}"
    _, cursor, _ = await drain(api, owner, scope, None)
    dsn = live_settings.api_database_dsn
    assert dsn is not None
    filler = [
        {
            "changed_at": "2026-10-06T00:00:00Z",
            "scope_type": "plan",
            "scope_id": plan["id"],
            "entity_type": "poll",
            "entity_id": str(uuid4()),
            "entity_version": 1,
            "operation": "upsert",
        }
        for _ in range(600)
    ]
    with (
        psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as connection,
        connection.transaction(),
    ):
        connection.execute("SELECT sync_audit.append_changes(%s::jsonb)", [json.dumps(filler)])
    renamed = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "After the filler"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert renamed.status_code == 200

    first = await pull_once(api, owner, scope, cursor, page_size=500)
    assert first["has_more"] is True and first["changes"] == []
    second = await pull_once(api, owner, scope, first["cursor"], page_size=500)
    assert second["has_more"] is False
    assert [(item["entity_type"], item["seq"]) for item in second["changes"]] == [("plan", 603)]
    oversized = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": scope, "cursor": cursor}], "page_size": 501},
        headers=owner.headers,
    )
    assert oversized.status_code == 422
