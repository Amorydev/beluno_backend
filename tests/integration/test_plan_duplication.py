"""Duplicating a plan copies only the allowlisted manifest."""

from __future__ import annotations

import httpx
import pytest

from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


async def test_duplicate_copies_only_the_allowlisted_manifest(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    friend = await sign_in(api, identity_provider, name="Friend")
    leaver = await sign_in(api, identity_provider, name="Leaver")
    source = (
        await api.post(
            "/v1/plans",
            json={
                "title": "Da Lat trip",
                "kind": "trip",
                "base_currency": "VND",
                "description": "Pack warm clothes",
                "location_label": "Hotel 12 Tran Phu",
                "participants": [{"placeholder_name": "Cousin"}],
            },
            headers=owner.headers,
        )
    ).json()
    invite = (
        await api.post(f"/v1/plans/{source['id']}/invites", json={}, headers=owner.headers)
    ).json()
    for user in (friend, leaver):
        await api.post("/v1/invites/redeem", json={"token": invite["token"]}, headers=user.headers)
    await api.post("/v1/invites/redeem", json={"token": invite["token"], "display_name": "Guest"})
    await api.post(f"/v1/plans/{source['id']}/leave", headers=leaver.headers)
    await api.put(
        f"/v1/plans/{source['id']}/rsvp", json={"status": "going"}, headers=friend.headers
    )
    for version, state in ((1, "active"), (2, "completed")):
        await api.post(
            f"/v1/plans/{source['id']}/state",
            json={"state": state},
            headers={**owner.headers, "If-Match": f'"{version}"'},
        )

    copied = await api.post(
        f"/v1/plans/{source['id']}/duplicate",
        json={"title": "Da Lat again"},
        headers=owner.headers,
    )
    assert copied.status_code == 201, copied.text
    plan = copied.json()
    assert (plan["title"], plan["state"], plan["kind"]) == ("Da Lat again", "planning", "trip")
    assert plan["description"] == "Pack warm clothes"
    assert plan["location_label"] is None
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    assert sorted((p["display_name"], p["role"], p["rsvp_status"]) for p in roster) == [
        ("Friend", "member", "invited"),
        ("Owner", "owner", "invited"),
    ]
    assert (await api.get(f"/v1/plans/{plan['id']}/invites", headers=owner.headers)).json() == []
    assert (
        admin.scalar(
            "SELECT metadata->>'manifest' FROM sync_audit.audit_events "
            "WHERE action = 'plan.duplicated'"
        )
        == "plan-copy-v1"
    )

    none_selected = await api.post(
        f"/v1/plans/{source['id']}/duplicate",
        json={"participant_ids": []},
        headers=owner.headers,
    )
    copied_roster = await api.get(
        f"/v1/plans/{none_selected.json()['id']}/participants", headers=owner.headers
    )
    assert [p["role"] for p in copied_roster.json()] == ["owner"]
    friend_view = await api.post(
        f"/v1/plans/{source['id']}/duplicate", json={}, headers=friend.headers
    )
    assert friend_view.status_code == 403
