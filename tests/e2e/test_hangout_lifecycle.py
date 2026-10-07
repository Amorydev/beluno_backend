"""A hangout end to end through the public API on real PostgreSQL."""

from __future__ import annotations

import re
from uuid import UUID

import httpx
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.email_challenges import deliver_challenge
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import RecordingEmailSender
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration


async def email_code(api: httpx.AsyncClient, settings: Settings, email: str) -> tuple[str, str]:
    started = await api.post("/v1/auth/email/challenges", json={"email": email})
    challenge_id = started.json()["challenge_id"]
    sender = RecordingEmailSender()
    database = Database.for_worker(settings)
    try:
        await deliver_challenge(
            Runtime(
                settings=settings,
                database=database,
                tokens=AccessTokenCodec(settings),
                hasher=TokenHasher.from_settings(settings),
                identity_verifier=ExternalIdentityVerifier(settings),
            ),
            UUID(challenge_id),
            sender,
        )
    finally:
        await database.close()
    match = re.search(r"code is (\d{6})", sender.messages[0].text_body)
    assert match is not None
    return challenge_id, match.group(1)


async def test_hangout_access_spine(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    live_settings: Settings,
    admin: AdminDatabase,
) -> None:
    # Organizer signs in passwordlessly; a friend uses Google.
    challenge_id, code = await email_code(api, live_settings, "linh@example.com")
    organizer = signed_in_from(
        (
            await api.post(
                "/v1/auth/email/verify",
                json={"challenge_id": challenge_id, "code": code, "display_name": "Linh"},
            )
        ).json()
    )
    friend = await sign_in(api, identity_provider, name="Minh")

    # A dinner the friend joins from a shared link, plus a guest who joins from the web.
    dinner = (
        await api.post(
            "/v1/plans",
            json={
                "type": "hangout",
                "title": "Hotpot",
                "activity": "dinner",
                "base_currency": "VND",
                "timing": {"mode": "date", "start_date": "2026-10-17"},
            },
            headers=organizer.headers,
        )
    ).json()
    link = await api.post(f"/v1/plans/{dinner['id']}/invites", json={}, headers=organizer.headers)
    joined = await api.post(
        "/v1/invites/redeem", json={"token": link.json()["token"]}, headers=friend.headers
    )
    assert joined.json()["participant"]["role"] == "member"
    guest_join = await api.post(
        "/v1/invites/redeem", json={"token": link.json()["token"], "display_name": "An"}
    )
    guest = signed_in_from(guest_join.json()["session"])
    guest_participant_id = guest_join.json()["participant"]["id"]
    for person, answer in ((friend, "going"), (guest, "maybe")):
        response = await api.put(
            f"/v1/plans/{dinner['id']}/rsvp", json={"status": answer}, headers=person.headers
        )
        assert response.status_code == 200

    # The guest creates an account by email OTP without changing identity.
    guest_challenge, guest_code = await email_code(api, live_settings, "an@example.com")
    upgraded = await api.post(
        "/v1/auth/email/verify",
        json={"challenge_id": guest_challenge, "code": guest_code},
        headers=guest.headers,
    )
    assert upgraded.json()["user"]["id"] == guest.user_id
    guest = SignedIn(
        user_id=guest.user_id,
        access_token=upgraded.json()["access_token"],
        refresh_token=guest.refresh_token,
        session_id=guest.session_id,
        profile=upgraded.json()["user"],
    )
    roster = (
        await api.get(f"/v1/plans/{dinner['id']}/participants", headers=organizer.headers)
    ).json()
    an = next(person for person in roster if person["id"] == guest_participant_id)
    assert (an["identity_kind"], an["role"], an["rsvp_status"]) == ("user", "member", "maybe")

    # Removing the upgraded guest revokes access at once; history stays.
    assert (
        await api.delete(
            f"/v1/plans/{dinner['id']}/participants/{guest_participant_id}",
            headers=organizer.headers,
        )
    ).status_code == 204
    assert (await api.get(f"/v1/plans/{dinner['id']}", headers=guest.headers)).status_code == 404

    # Ownership moves to the friend after a fresh sign-in; the organizer can then leave.
    minh = next(person for person in roster if person["display_name"] == "Minh")
    transfer = await api.post(
        f"/v1/plans/{dinner['id']}/ownership-transfer",
        json={"new_owner_participant_id": minh["id"]},
        headers={**organizer.headers, "If-Match": f'"{dinner["version"]}"'},
    )
    assert transfer.status_code == 200, transfer.text
    assert (
        await api.post(f"/v1/plans/{dinner['id']}/leave", headers=organizer.headers)
    ).status_code == 204

    # The new owner reuses the plan for next week; history and RSVPs do not carry over.
    again = await api.post(
        f"/v1/plans/{dinner['id']}/duplicate",
        json={"timing": {"mode": "date", "start_date": "2026-10-24"}},
        headers=friend.headers,
    )
    assert again.status_code == 201
    copy_roster = (
        await api.get(f"/v1/plans/{again.json()['id']}/participants", headers=friend.headers)
    ).json()
    assert [person["display_name"] for person in copy_roster] == ["Minh"]

    # Every accepted mutation left an audit event and a change-log entry; access
    # signals and feed events add change rows of their own without an audit event.
    events = admin.scalar("SELECT count(*) FROM sync_audit.audit_events")
    changes = admin.scalar("SELECT count(*) FROM sync_audit.change_log")
    signals = admin.scalar(
        "SELECT count(*) FROM sync_audit.change_log WHERE entity_type = 'plan_access'"
    )
    feed = admin.scalar("SELECT count(*) FROM activity.events")
    assert events > 20 and feed > 5
    assert changes == events + signals + feed
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.change_log WHERE entity_type = 'activity_event'"
        )
        == feed
    )
