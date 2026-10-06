"""Plans, stable participants, RSVP, and lifecycle."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import join_with_invite
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: object) -> dict:
    payload = {
        "type": "hangout",
        "title": "Dinner",
        "activity": "dinner",
        "base_currency": "VND",
        **body,
    }
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
    plan = await make_plan(api, owner, title="Movie night", activity="movie")

    assert plan["timing"] == {
        "mode": "undecided",
        "start_date": None,
        "end_date": None,
        "starts_at": None,
        "ends_at": None,
        "timezone": None,
    }
    assert plan["my_participant"]["role"] == "owner"
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


async def test_plan_validation(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    no_currency = await api.post("/v1/plans", json={"title": "Coffee"}, headers=owner.headers)
    assert no_currency.status_code == 422
    bad_timing = await api.post(
        "/v1/plans",
        json={
            "type": "hangout",
            "title": "Coffee",
            "base_currency": "USD",
            "timing": {"mode": "date", "start_date": "2026-10-10", "end_date": "2026-10-01"},
        },
        headers=owner.headers,
    )
    assert bad_timing.status_code == 422


async def test_new_plans_seed_people_from_shared_plans_and_placeholders(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    friend = await sign_in(api, identity_provider, name="Friend")
    stranger = await sign_in(api, identity_provider, name="Stranger")
    first = await make_plan(api, owner)
    await join_with_invite(api, owner, first["id"], friend)

    again = await make_plan(
        api,
        owner,
        participants=[{"user_id": friend.user_id}, {"placeholder_name": "Grandma"}],
    )
    people = await participants(api, owner, again["id"])
    assert sorted((p["display_name"], p["identity_kind"]) for p in people) == [
        ("Friend", "user"),
        ("Grandma", "placeholder"),
        ("Owner", "user"),
    ]
    # Someone who never shared a plan with the owner cannot be added directly.
    unknown = await api.post(
        "/v1/plans",
        json={
            "type": "hangout",
            "title": "Dinner",
            "base_currency": "VND",
            "participants": [{"user_id": stranger.user_id}],
        },
        headers=owner.headers,
    )
    assert unknown.status_code == 404


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
    plan = await make_plan(api, owner)
    await join_with_invite(api, owner, plan["id"], friend)

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
    plan = await make_plan(api, owner)
    await join_with_invite(api, owner, plan["id"], friend)
    friend_row = next(
        p for p in await participants(api, owner, plan["id"]) if p["user_id"] == friend.user_id
    )

    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{friend_row['id']}", headers=owner.headers
    )
    assert removed.status_code == 204
    for path in ("", "/participants"):
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


async def test_owner_must_transfer_before_leaving(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    await join_with_invite(api, owner, plan["id"], friend)
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
