"""Generic plans, stable participants, RSVP, lifecycle, and the travel extension."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

GROUP = {"name": "Crew", "default_currency": "VND", "default_timezone": "Asia/Ho_Chi_Minh"}


async def make_group(api: httpx.AsyncClient, owner: SignedIn) -> dict:
    response = await api.post("/v1/groups", json=GROUP, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


def seed_member(admin: AdminDatabase, group_id: str, user_id: str) -> None:
    admin.execute(
        "INSERT INTO groups.group_memberships (group_id, user_id, role, state, joined_at, version, "
        "created_at, updated_at) VALUES (%s, %s, 'member', 'active', now(), 1, now(), now())",
        group_id,
        user_id,
    )


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: object) -> dict:
    payload = {"title": "Dinner", "kind": "dinner", "base_currency": "VND", **body}
    response = await api.post("/v1/plans", json=payload, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def participants(api: httpx.AsyncClient, user: SignedIn, plan_id: str) -> list[dict]:
    response = await api.get(f"/v1/plans/{plan_id}/participants", headers=user.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_movie_plan_needs_no_travel_fields(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner, title="Movie night", kind="movie")

    assert plan["timing"] == {
        "mode": "undecided",
        "start_date": None,
        "end_date": None,
        "starts_at": None,
        "ends_at": None,
        "timezone": None,
    }
    assert plan["visibility"] == "participants"
    assert plan["my_participant"]["role"] == "owner"
    assert (
        await api.get(f"/v1/plans/{plan['id']}/travel", headers=owner.headers)
    ).status_code == 404
    listed = await api.get("/v1/plans", headers=owner.headers)
    assert [item["id"] for item in listed.json()["items"]] == [plan["id"]]
    timed = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={
            "timing": {
                "mode": "datetime",
                "starts_at": "2026-10-10T19:30:00+07:00",
                "timezone": "Asia/Ho_Chi_Minh",
            }
        },
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert timed.status_code == 200
    assert timed.json()["timing"]["starts_at"] == "2026-10-10T12:30:00Z"


async def test_plan_validation_without_group(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    no_currency = await api.post("/v1/plans", json={"title": "Coffee"}, headers=owner.headers)
    assert no_currency.status_code == 422
    group_visibility = await api.post(
        "/v1/plans",
        json={"title": "Coffee", "base_currency": "USD", "visibility": "group"},
        headers=owner.headers,
    )
    assert group_visibility.status_code == 422
    bad_timing = await api.post(
        "/v1/plans",
        json={
            "title": "Coffee",
            "base_currency": "USD",
            "timing": {"mode": "date", "start_date": "2026-10-10", "end_date": "2026-10-01"},
        },
        headers=owner.headers,
    )
    assert bad_timing.status_code == 422


async def test_group_plan_snapshots_members_and_defaults(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    friend = await sign_in(api, identity_provider, name="Friend")
    group = await make_group(api, owner)
    seed_member(admin, group["id"], friend.user_id)

    plan = await make_plan(
        api,
        owner,
        base_currency=None,
        group_id=group["id"],
        include_all_group_members=True,
        participants=[{"placeholder_name": "Grandma"}],
    )
    assert plan["base_currency"] == "VND"
    assert plan["visibility"] == "group"
    people = await participants(api, owner, plan["id"])
    assert sorted((p["display_name"], p["identity_kind"]) for p in people) == [
        ("Friend", "user"),
        ("Grandma", "placeholder"),
        ("Owner", "user"),
    ]

    await api.patch(
        f"/v1/groups/{group['id']}",
        json={"default_currency": "USD"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    unchanged = await api.get(f"/v1/plans/{plan['id']}", headers=owner.headers)
    assert unchanged.json()["base_currency"] == "VND"


async def test_participant_ids_are_scoped_to_their_plan(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    alice = await sign_in(api, identity_provider)
    bob = await sign_in(api, identity_provider)
    alice_plan = await make_plan(api, alice, participants=[{"placeholder_name": "Guest A"}])
    bob_plan = await make_plan(api, bob)
    alice_participant = next(
        p
        for p in await participants(api, alice, alice_plan["id"])
        if p["display_name"] == "Guest A"
    )

    # Bob manages his own plan but tries to act on Alice's participant through it.
    cross = await api.delete(
        f"/v1/plans/{bob_plan['id']}/participants/{alice_participant['id']}", headers=bob.headers
    )
    assert cross.status_code == 404
    direct = await api.get(f"/v1/plans/{alice_plan['id']}/participants", headers=bob.headers)
    assert direct.status_code == 404
    unknown = await api.get(f"/v1/plans/{uuid4()}", headers=bob.headers)
    hidden = await api.get(f"/v1/plans/{alice_plan['id']}", headers=bob.headers)
    assert unknown.json()["code"] == hidden.json()["code"] == "NOT_FOUND"


async def test_rsvp_is_independent_from_access(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    group = await make_group(api, owner)
    seed_member(admin, group["id"], friend.user_id)
    plan = await make_plan(api, owner, group_id=group["id"], include_all_group_members=True)

    for answer in ("going", "declined"):
        response = await api.put(
            f"/v1/plans/{plan['id']}/rsvp", json={"status": answer}, headers=friend.headers
        )
        assert response.status_code == 200
        assert response.json()["rsvp_status"] == answer
        assert response.json()["access_state"] == "active"
        assert response.json()["role"] == "member"
    still_visible = await api.get(f"/v1/plans/{plan['id']}", headers=friend.headers)
    assert still_visible.json()["my_participant"]["rsvp_status"] == "declined"
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.change_log WHERE scope_id = %s "
            "AND entity_type = 'plan_participant'",
            plan["id"],
        )
        >= 4
    )


async def test_removed_participant_loses_access_with_a_valid_token(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    group = await make_group(api, owner)
    seed_member(admin, group["id"], friend.user_id)
    plan = await make_plan(
        api, owner, group_id=group["id"], visibility="participants", include_all_group_members=True
    )
    friend_row = next(
        p for p in await participants(api, owner, plan["id"]) if p["user_id"] == friend.user_id
    )

    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{friend_row['id']}", headers=owner.headers
    )
    assert removed.status_code == 204
    for path in ("", "/participants", "/travel"):
        response = await api.get(f"/v1/plans/{plan['id']}{path}", headers=friend.headers)
        assert response.status_code == 404
    rsvp = await api.put(
        f"/v1/plans/{plan['id']}/rsvp", json={"status": "going"}, headers=friend.headers
    )
    assert rsvp.status_code == 404
    history = await api.get(
        f"/v1/plans/{plan['id']}/participants?include_inactive=true", headers=owner.headers
    )
    kept = next(p for p in history.json() if p["id"] == friend_row["id"])
    assert kept["access_state"] == "removed"

    readded = await api.post(
        f"/v1/plans/{plan['id']}/participants",
        json={"user_id": friend.user_id},
        headers=owner.headers,
    )
    assert readded.status_code == 201
    assert readded.json()["id"] == friend_row["id"]


async def test_group_members_can_view_and_join_group_visible_plans(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    member = await sign_in(api, identity_provider, name="Late Joiner")
    group = await make_group(api, owner)
    seed_member(admin, group["id"], member.user_id)
    open_plan = await make_plan(api, owner, group_id=group["id"])
    private_plan = await make_plan(api, owner, group_id=group["id"], visibility="participants")

    in_group = await api.get(f"/v1/plans?group_id={group['id']}", headers=member.headers)
    assert [item["id"] for item in in_group.json()["items"]] == [open_plan["id"]]
    assert (
        await api.get(f"/v1/plans/{private_plan['id']}", headers=member.headers)
    ).status_code == 404
    viewed = await api.get(f"/v1/plans/{open_plan['id']}", headers=member.headers)
    assert viewed.json()["my_participant"] is None
    edit = await api.patch(
        f"/v1/plans/{open_plan['id']}",
        json={"title": "x"},
        headers={**member.headers, "If-Match": '"1"'},
    )
    assert edit.status_code == 403

    joined = await api.post(f"/v1/plans/{open_plan['id']}/join", headers=member.headers)
    assert joined.status_code == 200
    assert (
        await api.post(f"/v1/plans/{open_plan['id']}/leave", headers=member.headers)
    ).status_code == 204
    rejoined = await api.post(f"/v1/plans/{open_plan['id']}/join", headers=member.headers)
    assert rejoined.json()["id"] == joined.json()["id"]


async def test_owner_must_transfer_before_leaving(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    group = await make_group(api, owner)
    seed_member(admin, group["id"], friend.user_id)
    plan = await make_plan(api, owner, group_id=group["id"], include_all_group_members=True)
    friend_row = next(
        p for p in await participants(api, owner, plan["id"]) if p["user_id"] == friend.user_id
    )

    blocked = await api.post(f"/v1/plans/{plan['id']}/leave", headers=owner.headers)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "OWNER_TRANSFER_REQUIRED"
    transferred = await api.post(
        f"/v1/plans/{plan['id']}/ownership-transfer",
        json={"new_owner_participant_id": friend_row["id"]},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert transferred.status_code == 200
    assert transferred.json()["my_participant"]["role"] == "admin"
    assert (
        await api.post(f"/v1/plans/{plan['id']}/leave", headers=owner.headers)
    ).status_code == 204


async def test_lifecycle_transitions_and_read_only_states(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    path = f"/v1/plans/{plan['id']}/state"

    invalid = await api.post(
        path, json={"state": "archived"}, headers={**owner.headers, "If-Match": '"1"'}
    )
    assert invalid.status_code == 409
    for version, state in ((1, "active"), (2, "completed")):
        moved = await api.post(
            path, json={"state": state}, headers={**owner.headers, "If-Match": f'"{version}"'}
        )
        assert moved.status_code == 200, moved.text
    frozen = await api.patch(
        f"/v1/plans/{plan['id']}", json={"title": "x"}, headers={**owner.headers, "If-Match": '"3"'}
    )
    assert frozen.status_code == 403

    admin.execute("UPDATE iam.sessions SET authenticated_at = now() - interval '1 hour'")
    step_up = await api.delete(
        f"/v1/plans/{plan['id']}", headers={**owner.headers, "If-Match": '"3"'}
    )
    assert step_up.json()["code"] == "STEP_UP_REQUIRED"
    admin.execute("UPDATE iam.sessions SET authenticated_at = now()")
    scheduled = await api.delete(
        f"/v1/plans/{plan['id']}", headers={**owner.headers, "If-Match": '"3"'}
    )
    assert scheduled.json()["deletion_scheduled_at"] is not None
    restored = await api.post(
        f"/v1/plans/{plan['id']}/restore", headers={**owner.headers, "If-Match": '"4"'}
    )
    assert restored.json()["deletion_scheduled_at"] is None


async def test_travel_segments_keep_local_times_across_dst(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner, title="London trip", kind="trip", base_currency="USD")
    travel = f"/v1/plans/{plan['id']}/travel"
    details = await api.put(travel, json={"destination_summary": "London"}, headers=owner.headers)
    assert details.status_code == 200
    assert details.headers["etag"] == '"1"'

    # Red-eye leaving New York during the repeated 01:30 hour of the DST fall-back.
    red_eye = await api.post(
        f"{travel}/segments",
        json={
            "segment_type": "flight",
            "timing_mode": "datetime",
            "departure_local": "2026-11-01T01:30:00",
            "departure_timezone": "America/New_York",
            "arrival_local": "2026-11-01T13:10:00",
            "arrival_timezone": "Europe/London",
        },
        headers=owner.headers,
    )
    assert red_eye.status_code == 201, red_eye.text
    assert red_eye.json()["departs_at"] == "2026-11-01T05:30:00Z"
    assert red_eye.json()["arrives_at"] == "2026-11-01T13:10:00Z"
    assert red_eye.json()["departure_local"] == "2026-11-01T01:30:00"

    gap = await api.post(
        f"{travel}/segments",
        json={
            "segment_type": "train",
            "timing_mode": "datetime",
            "departure_local": "2026-03-08T02:30:00",
            "departure_timezone": "America/New_York",
        },
        headers=owner.headers,
    )
    assert gap.status_code == 422
    hotel = await api.post(
        f"{travel}/segments",
        json={
            "segment_type": "lodging",
            "timing_mode": "date",
            "start_date": "2026-11-01",
            "end_date": "2026-11-05",
            "sort_order": 1,
        },
        headers=owner.headers,
    )
    assert hotel.status_code == 201
    listed = (await api.get(travel, headers=owner.headers)).json()
    assert [segment["segment_type"] for segment in listed["segments"]] == ["flight", "lodging"]
    removed = await api.delete(f"{travel}/segments/{hotel.json()['id']}", headers=owner.headers)
    assert removed.status_code == 204
    assert len((await api.get(travel, headers=owner.headers)).json()["segments"]) == 1
