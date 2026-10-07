"""Insiders cannot escalate through RLS, closed plans, stale invitations, or bearer links."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.modules.invite_links import token_digest
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import join_with_invite
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration


@pytest.fixture
def api_connection(live_settings: Settings) -> Iterator[psycopg.Connection]:
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as connection:
        yield connection


def run_as(
    connection: psycopg.Connection,
    actor: str,
    statement: str,
    *params: object,
    invite_hash: bytes = b"",
) -> int:
    with connection.transaction():
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor,))
        connection.execute(
            "SELECT set_config('app.invite_token_hash', %s, true)", (invite_hash.hex(),)
        )
        return connection.execute(statement, params).rowcount  # type: ignore[arg-type]


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: object) -> dict:
    response = await api.post(
        "/v1/plans",
        json={"type": "hangout", "title": "Plan", "base_currency": "USD", **body},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def redeem(api: httpx.AsyncClient, token: str, user: SignedIn) -> httpx.Response:
    return await api.post("/v1/invites/redeem", json={"token": token}, headers=user.headers)


async def test_insiders_cannot_promote_reactivate_or_move_rows(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    api_connection: psycopg.Connection,
    live_settings: Settings,
) -> None:
    owner = await sign_in(api, identity_provider)
    viewer = await sign_in(api, identity_provider)
    removed = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    invite = (
        await api.post(
            f"/v1/plans/{plan['id']}/invites", json={"role": "viewer"}, headers=owner.headers
        )
    ).json()
    viewer_row = (await redeem(api, invite["token"], viewer)).json()["participant"]
    removed_row = (await redeem(api, invite["token"], removed)).json()["participant"]
    await api.delete(
        f"/v1/plans/{plan['id']}/participants/{removed_row['id']}", headers=owner.headers
    )

    attempts = [
        (
            removed.user_id,
            "UPDATE plans.plan_participants SET access_state = 'active', "
            "role = 'admin' WHERE id = %s",
            removed_row["id"],
        ),
        (
            viewer.user_id,
            "UPDATE plans.plan_participants SET role = 'admin' WHERE id = %s",
            viewer_row["id"],
        ),
        (
            viewer.user_id,
            "UPDATE plans.plans SET created_by_user_id = %s WHERE id = %s",
            viewer.user_id,
            plan["id"],
        ),
        (viewer.user_id, "UPDATE plans.plans SET title = 'hijacked' WHERE id = %s", plan["id"]),
    ]
    for actor, statement, *params in attempts:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            run_as(api_connection, actor, statement, *params)

    hasher = TokenHasher.from_settings(live_settings)
    assert hasher is not None
    digest = token_digest(hasher, invite["token"])
    stranger = await sign_in(api, identity_provider)
    for statement in (
        "UPDATE plans.plan_invites SET role = 'admin', max_uses = NULL WHERE plan_id = %s",
        "UPDATE plans.plan_invites SET expires_at = now() + interval '1 year' WHERE plan_id = %s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            run_as(api_connection, stranger.user_id, statement, plan["id"], invite_hash=digest)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run_as(
            api_connection,
            stranger.user_id,
            "INSERT INTO plans.plan_participants (id, plan_id, identity_kind, user_id, "
            "display_name, role, access_state, rsvp_status, default_share, avatar_color, "
            "capabilities, version, created_at, updated_at) VALUES (gen_random_uuid(), %s, "
            "'user', %s, 'x', 'admin', 'active', 'invited', 100, 'blue', '{}', 1, now(), now())",
            plan["id"],
            stranger.user_id,
            invite_hash=digest,
        )


async def test_ownership_transfers_still_work_under_guards(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    friend = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    await join_with_invite(api, owner, plan["id"], friend)
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    target = next(row for row in roster if row["user_id"] == friend.user_id)

    plan_transfer = await api.post(
        f"/v1/plans/{plan['id']}/ownership-transfer",
        json={"new_owner_participant_id": target["id"]},
        headers={**owner.headers, "If-Match": f'"{plan["version"]}"'},
    )
    assert plan_transfer.status_code == 200


async def test_closed_plans_and_removed_placeholders_stop_admitting_people(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    late = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner, participants=[{"placeholder_name": "Cousin"}])
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    placeholder = next(row for row in roster if row["identity_kind"] == "placeholder")
    claim = await api.post(
        f"/v1/plans/{plan['id']}/participants/{placeholder['id']}/claim-invites",
        json={},
        headers=owner.headers,
    )
    await api.delete(
        f"/v1/plans/{plan['id']}/participants/{placeholder['id']}", headers=owner.headers
    )
    assert (await redeem(api, claim.json()["token"], late)).json()["code"] == "INVITE_UNAVAILABLE"

    join = (
        await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    ).json()
    await api.post(
        f"/v1/plans/{plan['id']}/state",
        json={"state": "cancelled"},
        headers={**owner.headers, "If-Match": f'"{plan["version"]}"'},
    )
    assert (await redeem(api, join["token"], late)).json()["code"] == "INVITE_UNAVAILABLE"
    preview = await api.post("/v1/invites/preview", json={"token": join["token"]})
    assert preview.status_code == 404


async def test_bearer_links_never_grant_management_rights(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    admin_placeholder = await api.post(
        "/v1/plans",
        json={
            "type": "hangout",
            "title": "Plan",
            "base_currency": "USD",
            "participants": [{"placeholder_name": "Boss", "role": "admin"}],
        },
        headers=owner.headers,
    )
    assert admin_placeholder.status_code == 422

    plan = await make_plan(api, owner, participants=[{"placeholder_name": "Cousin"}])
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    placeholder = next(row for row in roster if row["identity_kind"] == "placeholder")
    claim = await api.post(
        f"/v1/plans/{plan['id']}/participants/{placeholder['id']}/claim-invites",
        json={},
        headers=owner.headers,
    )
    guest = await api.post(
        "/v1/invites/redeem", json={"token": claim.json()["token"], "display_name": "Guest"}
    )
    assert guest.json()["participant"]["role"] == "guest"
    guest_headers = {"Authorization": f"Bearer {guest.json()['session']['access_token']}"}
    edit = await api.patch(
        f"/v1/plans/{plan['id']}",
        json={"title": "Mine now"},
        headers={**guest_headers, "If-Match": '"1"'},
    )
    assert edit.status_code == 403


async def test_rotation_and_input_hardening(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    invite = (
        await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    ).json()
    admin.execute("UPDATE plans.plan_invites SET expires_at = now() - interval '1 minute'")
    rotated = await api.post(
        f"/v1/plans/{plan['id']}/invites/{invite['id']}/rotate", headers=owner.headers
    )
    assert rotated.status_code == 409

    oversized = await api.get("/v1/me", headers={**owner.headers, "X-Request-ID": "x" * 500})
    assert oversized.status_code == 200
    assert len(oversized.headers["x-request-id"]) == 36
    safe = await api.get("/v1/me", headers={**owner.headers, "X-Request-ID": "trace-123"})
    assert safe.headers["x-request-id"] == "trace-123"
    renamed = await api.patch(
        "/v1/me",
        json={"display_name": "Renamed"},
        headers={**owner.headers, "If-Match": '"1"', "X-Request-ID": "y" * 300},
    )
    assert renamed.status_code == 200

    naive_time = await api.post(
        "/v1/plans",
        json={
            "type": "hangout",
            "title": "Dinner",
            "base_currency": "USD",
            "timing": {
                "mode": "datetime",
                "starts_at": "2027-01-04T19:00:00",
                "timezone": "Asia/Ho_Chi_Minh",
            },
        },
        headers=owner.headers,
    )
    assert naive_time.status_code == 422
