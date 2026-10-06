"""Invite links on real PostgreSQL: hashing, uniform errors, concurrency, approval."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from beluno.api.main import create_app
from beluno.config import Settings
from beluno.db.session import Database
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def make_plan(api: httpx.AsyncClient, owner: SignedIn) -> dict:
    response = await api.post(
        "/v1/plans",
        json={"title": "Birthday", "kind": "birthday", "base_currency": "EUR"},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_invite(
    api: httpx.AsyncClient, owner: SignedIn, plan_id: str, **body: object
) -> dict:
    response = await api.post(f"/v1/plans/{plan_id}/invites", json=body, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def redeem(
    api: httpx.AsyncClient, token: str, user: SignedIn | None = None, **body: object
) -> httpx.Response:
    headers = user.headers if user else {}
    return await api.post("/v1/invites/redeem", json={"token": token, **body}, headers=headers)


def problem_shape(response: httpx.Response) -> tuple[int, str, str, str | None]:
    body = response.json()
    return response.status_code, body["code"], body["title"], body.get("detail")


async def test_invite_is_hashed_previewed_minimally_and_redeemed_once(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Organizer")
    friend = await sign_in(api, identity_provider, name="Friend")
    plan = await make_plan(api, owner)
    invite = await make_invite(api, owner, plan["id"])
    token = invite["token"]

    stored = admin.fetch("SELECT token_hash FROM plans.plan_invites")
    assert token.encode() not in stored[0][0]
    assert token not in str(admin.fetch("SELECT metadata::text FROM sync_audit.audit_events"))

    preview = await api.post("/v1/invites/preview", json={"token": token})
    assert preview.status_code == 200
    assert preview.json()["plan"] == {
        "title": "Birthday",
        "kind": "birthday",
        "timing": {
            "mode": "undecided",
            "start_date": None,
            "end_date": None,
            "starts_at": None,
            "ends_at": None,
            "timezone": None,
        },
        "organizer_name": "Organizer",
    }
    assert "id" not in preview.json()["plan"]

    first = await redeem(api, token, friend)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "active"
    assert first.json()["plan"]["id"] == plan["id"]
    again = await redeem(api, token, friend)
    assert again.json()["participant"]["id"] == first.json()["participant"]["id"]
    listed = await api.get(f"/v1/plans/{plan['id']}/invites", headers=owner.headers)
    assert listed.json()[0]["use_count"] == 1
    assert "token" not in listed.json()[0]


async def test_unusable_invites_are_indistinguishable(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    expired = await make_invite(api, owner, plan["id"])
    revoked = await make_invite(api, owner, plan["id"])
    exhausted = await make_invite(api, owner, plan["id"], max_uses=1)
    admin.execute(
        "UPDATE plans.plan_invites SET expires_at = now() - interval '1 minute' WHERE id = %s",
        expired["id"],
    )
    await api.delete(f"/v1/plans/{plan['id']}/invites/{revoked['id']}", headers=owner.headers)
    assert (await redeem(api, exhausted["token"], friend)).status_code == 200

    shapes = set()
    for token in ("x" * 43, expired["token"], revoked["token"]):
        for path in ("/v1/invites/preview", "/v1/invites/redeem"):
            shapes.add(
                problem_shape(await api.post(path, json={"token": token}, headers=owner.headers))
            )
    stranger = await sign_in(api, identity_provider)
    shapes.add(problem_shape(await redeem(api, exhausted["token"], stranger)))
    assert shapes == {
        (
            404,
            "INVITE_UNAVAILABLE",
            "Invite is not available",
            "This invite link is invalid or no longer active",
        )
    }


async def test_concurrent_redemptions_of_the_last_use_admit_exactly_one(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    invite = await make_invite(api, owner, plan["id"], max_uses=1)
    racers = [await sign_in(api, identity_provider) for _ in range(4)]

    responses = await asyncio.gather(*(redeem(api, invite["token"], user) for user in racers))

    assert sorted(response.status_code for response in responses) == [200, 404, 404, 404]
    assert admin.scalar("SELECT use_count FROM plans.plan_invites") == 1
    assert (
        admin.scalar("SELECT count(*) FROM plans.plan_participants WHERE plan_id = %s", plan["id"])
        == 2
    )


async def test_approval_queue_keeps_requesters_out_until_approved(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    requester = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    invite = await make_invite(api, owner, plan["id"], requires_approval=True)

    pending = await redeem(api, invite["token"], requester)
    assert pending.json()["status"] == "pending_approval"
    assert pending.json()["plan"] is None
    assert (await api.get(f"/v1/plans/{plan['id']}", headers=requester.headers)).status_code == 404
    queue = await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)
    request_row = next(p for p in queue.json() if p["access_state"] == "pending_approval")

    approved = await api.post(
        f"/v1/plans/{plan['id']}/participants/{request_row['id']}/review",
        json={"approve": True},
        headers=owner.headers,
    )
    assert approved.json()["access_state"] == "active"
    assert (await api.get(f"/v1/plans/{plan['id']}", headers=requester.headers)).status_code == 200


async def test_email_bound_invite_only_admits_that_address(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    intended = await sign_in(api, identity_provider, email="hoa@example.com")
    other = await sign_in(api, identity_provider, email="lan@example.com")
    plan = await make_plan(api, owner)
    invite = await make_invite(api, owner, plan["id"], intended_email="Hoa@Example.com")

    assert (await redeem(api, invite["token"], display_name="Anon")).status_code == 401
    mismatch = await redeem(api, invite["token"], other)
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "INVITE_EMAIL_MISMATCH"
    assert (await redeem(api, invite["token"], intended)).status_code == 200


async def test_role_limits_removed_users_and_rotation(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    admin_user = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    admin_invite = await make_invite(api, owner, plan["id"], role="admin")
    assert (await redeem(api, admin_invite["token"], admin_user)).json()["participant"][
        "role"
    ] == "admin"
    by_admin = await api.post(
        f"/v1/plans/{plan['id']}/invites", json={"role": "admin"}, headers=admin_user.headers
    )
    assert by_admin.status_code == 403

    invite = await make_invite(api, owner, plan["id"])
    joined = await redeem(api, invite["token"], friend)
    await api.delete(
        f"/v1/plans/{plan['id']}/participants/{joined.json()['participant']['id']}",
        headers=owner.headers,
    )
    assert (await redeem(api, invite["token"], friend)).status_code == 403

    rotated = await api.post(
        f"/v1/plans/{plan['id']}/invites/{invite['id']}/rotate", headers=owner.headers
    )
    assert rotated.status_code == 201
    assert (
        await api.post("/v1/invites/preview", json={"token": invite["token"]})
    ).status_code == 404
    preview = await api.post("/v1/invites/preview", json={"token": rotated.json()["token"]})
    assert preview.status_code == 200


async def test_group_invite_links_add_registered_members(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider, name="Friend")
    group = (
        await api.post(
            "/v1/groups",
            json={"name": "Hikers", "default_currency": "USD", "default_timezone": "UTC"},
            headers=owner.headers,
        )
    ).json()
    invite = await api.post(f"/v1/groups/{group['id']}/invites", json={}, headers=owner.headers)
    token = invite.json()["token"]

    preview = await api.post("/v1/invites/preview", json={"token": token})
    assert preview.json()["group"] == {"name": "Hikers"}
    assert (await redeem(api, token, display_name="Guest")).status_code == 401
    joined = await redeem(api, token, friend)
    assert joined.json()["group"]["my_role"] == "member"
    members = await api.get(f"/v1/groups/{group['id']}/members", headers=owner.headers)
    assert {member["display_name"] for member in members.json()} == {"Test Member", "Friend"}

    await api.delete(
        f"/v1/groups/{group['id']}/invites/{invite.json()['id']}", headers=owner.headers
    )
    late = await sign_in(api, identity_provider)
    assert (await redeem(api, token, late)).status_code == 404


@pytest.fixture
async def invites_disabled_api(
    live_settings: Settings, identity_provider: IdentityProviderStub
) -> AsyncIterator[httpx.AsyncClient]:
    settings = live_settings.model_copy(update={"invites_enabled": False})
    database = Database(settings)
    app = create_app(
        settings=settings, database=database, identity_verifier=identity_provider.verifier(settings)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client
    await database.close()


async def test_kill_switch_disables_invite_entrypoints(
    invites_disabled_api: httpx.AsyncClient,
) -> None:
    for path in ("/v1/invites/preview", "/v1/invites/redeem"):
        response = await invites_disabled_api.post(path, json={"token": "y" * 43})
        assert response.status_code == 503
        assert response.json()["code"] == "FEATURE_DISABLED"
