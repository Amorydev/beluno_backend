"""Passkeys sign in and step up an existing account, and refuse anything off-pattern."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from webauthn.helpers import bytes_to_base64url

from beluno.testkit.api_client import SignedIn, bearer, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, finance_plan
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.passkeys import SoftAuthenticator

pytestmark = pytest.mark.integration


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def add_passkey(
    api: httpx.AsyncClient, user: SignedIn, device: SoftAuthenticator, label: str = "iPhone 15"
) -> dict[str, Any]:
    options = await ok(await api.post("/v1/me/passkeys/registration-options", headers=user.headers))
    added: dict[str, Any] = await ok(
        await api.post(
            "/v1/me/passkeys",
            json={
                "challenge_id": options["challenge_id"],
                "credential": device.create(options["public_key"]),
                "label": label,
            },
            headers=user.headers,
        ),
        201,
    )
    return added


async def options_for(api: httpx.AsyncClient, user: SignedIn | None = None) -> dict[str, Any]:
    headers = user.headers if user else {}
    found: dict[str, Any] = await ok(await api.post("/v1/auth/passkey/options", headers=headers))
    return found


async def use_passkey(
    api: httpx.AsyncClient,
    options: dict[str, Any],
    credential: dict[str, Any],
    user: SignedIn | None = None,
    **extra: Any,
) -> httpx.Response:
    return await api.post(
        "/v1/auth/passkey",
        json={"challenge_id": options["challenge_id"], "credential": credential, **extra},
        headers=user.headers if user else {},
    )


def stale(admin: AdminDatabase, user: SignedIn) -> None:
    """Make the session's last sign-in older than the step-up window."""

    admin.execute(
        "UPDATE iam.sessions SET authenticated_at = now() - interval '1 hour' WHERE id = %s",
        user.session_id,
    )


async def test_a_passkey_signs_in_steps_up_and_is_managed(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    ann = await sign_in(api, identity_provider, name="Ann", email="ann@example.com")
    phone = SoftAuthenticator()
    added = await add_passkey(api, ann, phone)
    assert (added["label"], added["backed_up"], added["last_used_at"]) == (
        "iPhone 15",
        False,
        None,
    )
    assert added["transports"] == ["hybrid", "internal"]
    options = await options_for(api)
    assert options["public_key"]["userVerification"] == "required"
    assert options["public_key"].get("allowCredentials", []) == []

    # Signed out, the passkey alone starts a session for its account.
    response = await use_passkey(api, options, phone.get(options["public_key"]))
    session = signed_in_from(await ok(response))
    assert session.user_id == ann.user_id and response.json()["refresh_token"]
    assert (
        admin.scalar("SELECT auth_method FROM iam.sessions WHERE id = %s", session.session_id)
        == "passkey"
    )
    # The same response (and challenge) never works twice.
    assert (await use_passkey(api, options, phone.get(options["public_key"]))).status_code == 401

    # Removing it needs a recent sign-in; the passkey itself provides one.
    stale(admin, session)
    path = f"/v1/me/passkeys/{added['id']}"
    refused = await api.delete(path, headers=session.headers)
    assert refused.status_code == 403 and refused.json()["code"] == "STEP_UP_REQUIRED"
    mine = await options_for(api, session)
    assert [c["id"] for c in mine["public_key"]["allowCredentials"]] == [added_credential(phone)]
    stepped = await ok(await use_passkey(api, mine, phone.get(mine["public_key"]), session))
    assert stepped["refresh_token"] is None and stepped["session_id"] == session.session_id

    renamed = await ok(await api.patch(path, json={"label": "Work phone"}, headers=session.headers))
    assert renamed["label"] == "Work phone"
    listed = await ok(await api.get("/v1/me/passkeys", headers=session.headers))
    assert [(row["id"], row["label"]) for row in listed] == [(added["id"], "Work phone")]
    assert listed[0]["last_used_at"] is not None
    bea = await sign_in(api, identity_provider, name="Bea")
    assert (await api.delete(path, headers=bea.headers)).status_code == 404
    assert (await api.patch(path, json={"label": "x"}, headers=bea.headers)).status_code == 404
    assert (await api.delete(path, headers=session.headers)).status_code == 204
    gone = await options_for(api)
    assert (await use_passkey(api, gone, phone.get(gone["public_key"]))).status_code == 401
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events "
            "WHERE action IN ('iam.passkey_added', 'iam.passkey_removed') AND entity_id = %s",
            added["id"],
        )
        == 2
    )


def added_credential(device: SoftAuthenticator) -> str:
    return bytes_to_base64url(device.credential_id)


async def test_responses_that_do_not_hold_up_are_refused(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    ann = await sign_in(api, identity_provider, name="Ann")
    phone = SoftAuthenticator()
    await add_passkey(api, ann, phone)

    async def attempt(**variant: Any) -> int:
        options = await options_for(api)
        response = await use_passkey(api, options, phone.get(options["public_key"], **variant))
        return response.status_code

    # No user verification, another origin, or a counter that went back (a clone).
    assert await attempt(verified=False) == 401
    assert await attempt(origin="https://evil.example") == 401
    assert await attempt() == 200
    assert await attempt(sign_count=1) == 401
    # A failed response still used up its challenge.
    options = await options_for(api)
    bad = phone.get(options["public_key"], verified=False)
    assert (await use_passkey(api, options, bad)).status_code == 401
    # (A counter well ahead of the stored one: only the used-up challenge refuses this.)
    good = phone.get(options["public_key"], sign_count=1_000)
    assert (await use_passkey(api, options, good)).status_code == 401
    assert admin.scalar(
        "SELECT consumed_at IS NOT NULL FROM iam.webauthn_challenges WHERE id = %s",
        options["challenge_id"],
    )
    # Expired, or issued to someone else.
    late = await options_for(api)
    admin.execute(
        "UPDATE iam.webauthn_challenges SET expires_at = now() - interval '1 second' WHERE id = %s",
        late["challenge_id"],
    )
    assert (await use_passkey(api, late, phone.get(late["public_key"]))).status_code == 401
    bea = await sign_in(api, identity_provider, name="Bea")
    hers = await options_for(api, bea)
    assert (await use_passkey(api, hers, phone.get(hers["public_key"]))).status_code == 401
    # Stepping up someone else's session with your passkey is refused outright.
    theirs = await options_for(api, bea)
    mismatch = await use_passkey(api, theirs, phone.get(theirs["public_key"]), bea)
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "REAUTHENTICATION_MISMATCH"

    # A synced passkey reports no counter at all, every time.
    synced = SoftAuthenticator(synced=True, counts=False)
    added = await add_passkey(api, bea, synced, label="Pixel")
    assert added["backed_up"] is True
    for _ in range(2):
        options = await options_for(api)
        await ok(await use_passkey(api, options, synced.get(options["public_key"])))


async def test_adding_a_passkey_needs_an_account_and_a_recent_sign_in(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    ann = await sign_in(api, identity_provider, name="Ann")
    stale(admin, ann)
    refused = await api.post("/v1/me/passkeys/registration-options", headers=ann.headers)
    assert refused.status_code == 403 and refused.json()["code"] == "STEP_UP_REQUIRED"

    fresh = await sign_in(api, identity_provider, name="Bea")
    options = await ok(
        await api.post("/v1/me/passkeys/registration-options", headers=fresh.headers)
    )
    assert options["public_key"]["authenticatorSelection"]["residentKey"] == "required"
    assert options["public_key"]["attestation"] == "none"
    unverified = SoftAuthenticator().create(options["public_key"], verified=False)
    response = await api.post(
        "/v1/me/passkeys",
        json={"challenge_id": options["challenge_id"], "credential": unverified, "label": "x"},
        headers=fresh.headers,
    )
    assert response.status_code == 422
    # The challenge went with that attempt.
    retry = await api.post(
        "/v1/me/passkeys",
        json={
            "challenge_id": options["challenge_id"],
            "credential": SoftAuthenticator().create(options["public_key"]),
            "label": "x",
        },
        headers=fresh.headers,
    )
    assert retry.status_code == 422
    # Odd transports are ignored; a credential ID shorter than WebAuthn allows is refused.
    odd = await ok(await api.post("/v1/me/passkeys/registration-options", headers=fresh.headers))
    credential = SoftAuthenticator().create(odd["public_key"])
    credential["response"]["transports"] = 5
    kept = await ok(
        await api.post(
            "/v1/me/passkeys",
            json={"challenge_id": odd["challenge_id"], "credential": credential, "label": "Key"},
            headers=fresh.headers,
        ),
        201,
    )
    assert kept["transports"] == []
    short = await ok(await api.post("/v1/me/passkeys/registration-options", headers=fresh.headers))
    refused_short = await api.post(
        "/v1/me/passkeys",
        json={
            "challenge_id": short["challenge_id"],
            "credential": SoftAuthenticator(credential_id_length=8).create(short["public_key"]),
            "label": "Short",
        },
        headers=fresh.headers,
    )
    assert refused_short.status_code == 422
    # The same device cannot be added twice.
    phone = SoftAuthenticator()
    await add_passkey(api, fresh, phone)
    again = await ok(await api.post("/v1/me/passkeys/registration-options", headers=fresh.headers))
    assert added_credential(phone) in [c["id"] for c in again["public_key"]["excludeCredentials"]]
    duplicate = await api.post(
        "/v1/me/passkeys",
        json={
            "challenge_id": again["challenge_id"],
            "credential": phone.create(again["public_key"]),
            "label": "Twice",
        },
        headers=fresh.headers,
    )
    assert duplicate.status_code == 409


async def test_a_guest_claims_the_passkeys_account_and_deletion_forgets_it(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip: FinancePlan = await finance_plan(api, identity_provider, admin, members=())
    fay = await sign_in(api, identity_provider, name="Fay")
    phone = SoftAuthenticator()
    await add_passkey(api, fay, phone)

    invite = await ok(
        await api.post(trip.path("/invites"), json={}, headers=trip.owner.headers), 201
    )
    joined = await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": invite["token"], "display_name": "Gia", "device": {"platform": "web"}},
        )
    )
    guest = signed_in_from(joined["session"])
    assert (
        await api.post("/v1/me/passkeys/registration-options", headers=guest.headers)
    ).status_code == 403
    options = await options_for(api, guest)
    claimed = signed_in_from(
        await ok(await use_passkey(api, options, phone.get(options["public_key"]), guest))
    )
    assert claimed.user_id == fay.user_id
    plan = await ok(await api.get(trip.path(), headers=claimed.headers))
    assert plan["my_participant"]["id"] == joined["participant"]["id"]
    assert (await api.get("/v1/me", headers=bearer(guest.access_token))).status_code == 401

    gone = await api.delete("/v1/me", headers=claimed.headers)
    assert gone.status_code == 204, gone.text
    assert admin.scalar("SELECT count(*) FROM iam.passkeys WHERE user_id = %s", fay.user_id) == 0


async def test_a_refused_sign_in_never_leaves_its_response_reusable(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip: FinancePlan = await finance_plan(api, identity_provider, admin, members=())
    phone = SoftAuthenticator()
    await add_passkey(api, trip.owner, phone)
    invite = await ok(
        await api.post(trip.path("/invites"), json={}, headers=trip.owner.headers), 201
    )
    joined = await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": invite["token"], "display_name": "Gia", "device": {"platform": "web"}},
        )
    )
    guest = signed_in_from(joined["session"])

    # The account is already in the plan: the guest must consent to merging first.
    options = await options_for(api, guest)
    assertion = phone.get(options["public_key"])
    refused = await use_passkey(api, options, assertion, guest)
    assert refused.status_code == 409
    assert refused.json()["code"] == "PARTICIPANT_MERGE_REQUIRED"
    # The challenge is used up, the counter moved on, and nobody can send it again.
    assert admin.scalar(
        "SELECT consumed_at IS NOT NULL FROM iam.webauthn_challenges WHERE id = %s",
        options["challenge_id"],
    )
    assert (
        admin.scalar("SELECT sign_count FROM iam.passkeys WHERE user_id = %s", trip.owner.user_id)
        == phone.sign_count
    )
    assert (await use_passkey(api, options, assertion)).status_code == 401
    assert (await use_passkey(api, options, assertion, guest)).status_code == 401
    # A guest's challenge is the guest's: signed out, it is refused.
    theirs = await options_for(api, guest)
    assert (await use_passkey(api, theirs, phone.get(theirs["public_key"]))).status_code == 401

    consented = await options_for(api, guest)
    merged = signed_in_from(
        await ok(
            await use_passkey(
                api,
                consented,
                phone.get(consented["public_key"]),
                guest,
                merge_guest_participations=True,
            )
        )
    )
    assert merged.user_id == trip.owner.user_id
