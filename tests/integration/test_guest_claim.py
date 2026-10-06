"""Guest identities and claims keep participant IDs stable on real PostgreSQL."""

from __future__ import annotations

import httpx
import pytest

from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: object) -> dict:
    response = await api.post(
        "/v1/plans",
        json={"title": "Dinner", "kind": "dinner", "base_currency": "VND", **body},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def invite_token(
    api: httpx.AsyncClient, owner: SignedIn, plan_id: str, **body: object
) -> str:
    response = await api.post(f"/v1/plans/{plan_id}/invites", json=body, headers=owner.headers)
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


async def join_as_guest(
    api: httpx.AsyncClient, token: str, name: str = "Guest Gia"
) -> tuple[SignedIn, dict]:
    response = await api.post(
        "/v1/invites/redeem",
        json={"token": token, "display_name": name, "device": {"platform": "web"}},
    )
    assert response.status_code == 200, response.text
    return signed_in_from(response.json()["session"]), response.json()["participant"]


async def test_guest_joins_with_limited_rights(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    guest, participant = await join_as_guest(api, await invite_token(api, owner, plan["id"]))

    assert guest.profile["kind"] == "guest"
    assert participant["identity_kind"] == "guest"
    assert participant["role"] == "guest"
    rsvp = await api.put(
        f"/v1/plans/{plan['id']}/rsvp", json={"status": "going"}, headers=guest.headers
    )
    assert rsvp.status_code == 200
    denied_plan = await api.post(
        "/v1/plans", json={"title": "Mine", "base_currency": "USD"}, headers=guest.headers
    )
    assert denied_plan.status_code == 403
    denied_invite = await api.post(
        f"/v1/plans/{plan['id']}/invites", json={}, headers=guest.headers
    )
    assert denied_invite.status_code == 403
    group = await api.post(
        "/v1/groups",
        json={"name": "x", "default_currency": "USD", "default_timezone": "UTC"},
        headers=guest.headers,
    )
    assert group.status_code == 403


async def test_invites_without_guest_access_require_sign_in(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    token = await invite_token(api, owner, plan["id"], allow_guests=False)
    response = await api.post("/v1/invites/redeem", json={"token": token, "display_name": "Anon"})
    assert response.status_code == 401
    missing_name = await api.post(
        "/v1/invites/redeem", json={"token": await invite_token(api, owner, plan["id"])}
    )
    assert missing_name.status_code == 422


async def test_guest_upgrade_keeps_user_and_participant_ids(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner)
    guest, participant = await join_as_guest(api, await invite_token(api, owner, plan["id"]))

    upgraded = await api.post(
        "/v1/auth/google",
        json={"id_token": identity_provider.id_token(email="gia@example.com")},
        headers=guest.headers,
    )
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["user"]["id"] == guest.user_id
    assert upgraded.json()["user"]["kind"] == "registered"
    assert upgraded.json()["session_id"] == guest.session_id
    row = admin.fetch(
        "SELECT id::text, identity_kind, role FROM plans.plan_participants WHERE user_id = %s",
        guest.user_id,
    )
    assert row == [(participant["id"], "user", "member")]
    refreshed = await api.post("/v1/auth/refresh", json={"refresh_token": guest.refresh_token})
    assert refreshed.json()["user"]["kind"] == "registered"


async def test_guest_claiming_an_account_already_in_the_plan_requires_merge(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    member = await sign_in(api, identity_provider, subject="member-sub", name="Member")
    plan = await make_plan(api, owner)
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": await invite_token(api, owner, plan["id"])},
        headers=member.headers,
    )
    member_participant = joined.json()["participant"]
    guest, guest_participant = await join_as_guest(api, await invite_token(api, owner, plan["id"]))

    claim = {"id_token": identity_provider.id_token(subject="member-sub")}
    needs_consent = await api.post("/v1/auth/google", json=claim, headers=guest.headers)
    assert needs_consent.status_code == 409
    assert needs_consent.json()["code"] == "PARTICIPANT_MERGE_REQUIRED"

    merged = await api.post(
        "/v1/auth/google",
        json={**claim, "merge_guest_participations": True},
        headers=guest.headers,
    )
    assert merged.status_code == 200
    assert merged.json()["user"]["id"] == member.user_id
    rows = dict(
        admin.fetch(
            "SELECT id::text, access_state || ':' "
            "|| coalesce(merged_into_participant_id::text, '') "
            "FROM plans.plan_participants WHERE plan_id = %s",
            plan["id"],
        )
    )
    assert rows[guest_participant["id"]] == f"merged:{member_participant['id']}"
    assert rows[member_participant["id"]] == "active:"
    assert (await api.get("/v1/me", headers=guest.headers)).status_code == 401


async def test_guest_claiming_an_account_outside_the_plan_relinks_the_participant(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider)
    existing = await sign_in(api, identity_provider, subject="existing-sub")
    plan = await make_plan(api, owner)
    guest, guest_participant = await join_as_guest(api, await invite_token(api, owner, plan["id"]))

    claimed = await api.post(
        "/v1/auth/google",
        json={"id_token": identity_provider.id_token(subject="existing-sub")},
        headers=guest.headers,
    )
    assert claimed.status_code == 200
    after = signed_in_from(claimed.json())
    assert after.user_id == existing.user_id
    plan_view = await api.get(f"/v1/plans/{plan['id']}", headers=after.headers)
    assert plan_view.json()["my_participant"]["id"] == guest_participant["id"]
    assert admin.scalar("SELECT status FROM iam.users WHERE id = %s", guest.user_id) == "disabled"


async def test_placeholder_claim_links_the_same_participant(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    claimer = await sign_in(api, identity_provider, name="Real Grandma")
    plan = await make_plan(api, owner, participants=[{"placeholder_name": "Grandma"}])
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    placeholder = next(p for p in roster if p["identity_kind"] == "placeholder")

    claim = await api.post(
        f"/v1/plans/{plan['id']}/participants/{placeholder['id']}/claim-invites",
        json={},
        headers=owner.headers,
    )
    token = claim.json()["token"]
    preview = await api.post("/v1/invites/preview", json={"token": token})
    assert preview.json()["purpose"] == "claim"
    assert preview.json()["placeholder_name"] == "Grandma"

    redeemed = await api.post("/v1/invites/redeem", json={"token": token}, headers=claimer.headers)
    assert redeemed.status_code == 200
    assert redeemed.json()["participant"]["id"] == placeholder["id"]
    assert redeemed.json()["participant"]["identity_kind"] == "user"
    assert redeemed.json()["participant"]["display_name"] == "Grandma"
    second = await sign_in(api, identity_provider)
    reuse = await api.post("/v1/invites/redeem", json={"token": token}, headers=second.headers)
    assert reuse.status_code == 404


async def test_placeholder_claim_by_existing_participant_merges_on_consent(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    plan = await make_plan(api, owner, participants=[{"placeholder_name": "Owner's alias"}])
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    placeholder = next(p for p in roster if p["identity_kind"] == "placeholder")
    token = (
        await api.post(
            f"/v1/plans/{plan['id']}/participants/{placeholder['id']}/claim-invites",
            json={},
            headers=owner.headers,
        )
    ).json()["token"]

    blocked = await api.post("/v1/invites/redeem", json={"token": token}, headers=owner.headers)
    assert blocked.json()["code"] == "PARTICIPANT_MERGE_REQUIRED"
    merged = await api.post(
        "/v1/invites/redeem", json={"token": token, "merge_existing": True}, headers=owner.headers
    )
    assert merged.status_code == 200
    assert merged.json()["participant"]["role"] == "owner"
    history = await api.get(
        f"/v1/plans/{plan['id']}/participants?include_inactive=true", headers=owner.headers
    )
    merged_row = next(p for p in history.json() if p["id"] == placeholder["id"])
    assert merged_row["access_state"] == "merged"
    assert merged_row["merged_into_participant_id"] == merged.json()["participant"]["id"]
