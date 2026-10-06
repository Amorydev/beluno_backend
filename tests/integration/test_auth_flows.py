"""Self-hosted identity on real PostgreSQL: sign-in, sessions, refresh, email, step-up."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import jwt
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.email_challenges import deliver_challenge
from beluno.modules.iam.external_identity import ExternalIdentityVerifier, IdentityProvider
from beluno.testkit.api_client import bearer, sign_in, signed_in_from
from beluno.testkit.environment import IntegrationEnvironment, RecordingEmailSender
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration


def admin_execute(environment: IntegrationEnvironment, statement: str, *params: object) -> None:
    with psycopg.connect(environment.admin_dsn, autocommit=True) as connection:
        connection.execute(statement, params)


def admin_fetch(
    environment: IntegrationEnvironment, statement: str, *params: object
) -> list[tuple]:
    with psycopg.connect(environment.admin_dsn) as connection:
        return list(connection.execute(statement, params).fetchall())


async def deliver_pending_email(
    environment: IntegrationEnvironment, sender: RecordingEmailSender, challenge_id: str
) -> tuple[str, str]:
    settings = environment.settings
    database = Database.for_worker(settings)
    runtime = Runtime(
        settings=settings,
        database=database,
        tokens=AccessTokenCodec(settings),
        hasher=TokenHasher.from_settings(settings),
        identity_verifier=ExternalIdentityVerifier(settings),
    )
    try:
        from uuid import UUID

        assert await deliver_challenge(runtime, UUID(challenge_id), sender)
    finally:
        await database.close()
    body = sender.messages[-1].text_body
    code = re.search(r"code is (\d{6})", body)
    link = re.search(r"#token=([A-Za-z0-9_-]+)", body)
    assert code is not None and link is not None
    return code.group(1), link.group(1)


async def test_google_sign_in_creates_one_account_per_subject(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    first = await sign_in(api, identity_provider, subject="google-sub-1", email="ana@example.com")
    second = await sign_in(api, identity_provider, subject="google-sub-1", email="ana@example.com")

    assert first.user_id == second.user_id
    assert first.session_id != second.session_id
    assert first.profile["email"] == "ana@example.com"
    me = await api.get("/v1/me", headers=first.headers)
    assert me.status_code == 200
    assert me.headers["etag"] == '"1"'
    assert me.json()["display_name"] == "Test Member"


async def test_invalid_audience_and_nonce_are_rejected(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    wrong_audience = identity_provider.id_token(audience="someone-else")
    response = await api.post("/v1/auth/google", json={"id_token": wrong_audience})
    assert response.status_code == 401

    token = identity_provider.id_token(IdentityProvider.APPLE, nonce="expected-nonce-value")
    mismatch = await api.post(
        "/v1/auth/apple", json={"id_token": token, "nonce": "different-nonce-value"}
    )
    assert mismatch.status_code == 401
    accepted = await api.post(
        "/v1/auth/apple",
        json={"id_token": token, "nonce": "expected-nonce-value", "display_name": "Ana"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["user"]["display_name"] == "Ana"


async def test_access_token_is_verifiable_with_published_jwks(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    member = await sign_in(api, identity_provider)
    jwks = (await api.get("/.well-known/jwks.json")).json()
    key = jwt.PyJWK(jwks["keys"][0])

    claims = jwt.decode(member.access_token, key.key, algorithms=["ES256"], audience="beluno-api")

    assert claims["sub"] == member.user_id
    assert claims["sid"] == member.session_id
    assert claims["guest"] is False


async def test_refresh_rotates_and_reuse_revokes_the_session(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    environment: IntegrationEnvironment,
) -> None:
    member = await sign_in(api, identity_provider)
    rotated = await api.post("/v1/auth/refresh", json={"refresh_token": member.refresh_token})
    assert rotated.status_code == 200
    new_tokens = signed_in_from(rotated.json())
    assert new_tokens.refresh_token != member.refresh_token

    # Outside the lost-response grace window, replaying the consumed token is theft.
    admin_execute(
        environment,
        "UPDATE iam.refresh_tokens SET consumed_at = consumed_at - interval '1 hour' "
        "WHERE consumed_at IS NOT NULL",
    )
    replay = await api.post("/v1/auth/refresh", json={"refresh_token": member.refresh_token})
    assert replay.status_code == 401

    for token in (member.access_token, new_tokens.access_token):
        assert (await api.get("/v1/me", headers=bearer(token))).status_code == 401
    rejected = await api.post("/v1/auth/refresh", json={"refresh_token": new_tokens.refresh_token})
    assert rejected.status_code == 401
    reasons = admin_fetch(environment, "SELECT revoked_reason FROM iam.sessions")
    assert reasons == [("refresh_reuse",)]


async def test_lost_refresh_response_can_be_retried_within_grace(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    member = await sign_in(api, identity_provider)
    lost = await api.post("/v1/auth/refresh", json={"refresh_token": member.refresh_token})
    assert lost.status_code == 200

    retry = await api.post("/v1/auth/refresh", json={"refresh_token": member.refresh_token})
    assert retry.status_code == 200
    # The unseen replacement from the lost response is retired.
    stale = await api.post("/v1/auth/refresh", json={"refresh_token": lost.json()["refresh_token"]})
    assert stale.status_code == 401


async def test_logout_and_remote_revocation_take_effect_immediately(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    phone = await sign_in(api, identity_provider, subject="same-person", platform="ios")
    laptop = await sign_in(api, identity_provider, subject="same-person", platform="web")

    sessions = (await api.get("/v1/me/sessions", headers=laptop.headers)).json()
    assert {item["platform"] for item in sessions} == {"ios", "web"}
    revoke = await api.delete(f"/v1/me/sessions/{phone.session_id}", headers=laptop.headers)
    assert revoke.status_code == 204
    assert (await api.get("/v1/me", headers=phone.headers)).status_code == 401
    assert (await api.get("/v1/me", headers=laptop.headers)).status_code == 200

    assert (await api.post("/v1/auth/logout", headers=laptop.headers)).status_code == 204
    assert (await api.get("/v1/me", headers=laptop.headers)).status_code == 401
    other = await api.delete(f"/v1/me/sessions/{phone.session_id}", headers=laptop.headers)
    assert other.status_code == 401


async def test_email_code_sign_in_never_stores_or_queues_the_secret(
    api: httpx.AsyncClient,
    environment: IntegrationEnvironment,
    email_sender: RecordingEmailSender,
) -> None:
    started = await api.post("/v1/auth/email/challenges", json={"email": "Bao@Example.com"})
    assert started.status_code == 202
    challenge_id = started.json()["challenge_id"]
    jobs = admin_fetch(environment, "SELECT task_name, args FROM jobs.procrastinate_jobs")
    assert jobs == [
        ("iam.deliver_email_challenge", {"challenge_id": challenge_id, "payload_version": 1})
    ]

    code, _ = await deliver_pending_email(environment, email_sender, challenge_id)
    stored = admin_fetch(environment, "SELECT code_hash, email FROM iam.email_challenges")
    assert code.encode() not in stored[0][0]
    assert stored[0][1] == "bao@example.com"
    assert code not in json.dumps(jobs)

    verified = await api.post(
        "/v1/auth/email/verify",
        json={"challenge_id": challenge_id, "code": code, "display_name": "Bao"},
    )
    assert verified.status_code == 200
    assert verified.json()["user"]["email"] == "bao@example.com"
    assert verified.json()["user"]["display_name"] == "Bao"
    replay = await api.post(
        "/v1/auth/email/verify", json={"challenge_id": challenge_id, "code": code}
    )
    assert replay.status_code == 401


async def test_wrong_codes_exhaust_the_challenge(
    api: httpx.AsyncClient,
    environment: IntegrationEnvironment,
    email_sender: RecordingEmailSender,
) -> None:
    started = await api.post("/v1/auth/email/challenges", json={"email": "chi@example.com"})
    challenge_id = started.json()["challenge_id"]
    code, _ = await deliver_pending_email(environment, email_sender, challenge_id)
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(5):
        attempt = await api.post(
            "/v1/auth/email/verify", json={"challenge_id": challenge_id, "code": wrong}
        )
        assert attempt.status_code == 401
    correct = await api.post(
        "/v1/auth/email/verify", json={"challenge_id": challenge_id, "code": code}
    )
    assert correct.status_code == 401
    assert admin_fetch(environment, "SELECT failed_attempts FROM iam.email_challenges") == [(5,)]


async def test_magic_link_signs_in_once_and_google_is_linked_explicitly(
    api: httpx.AsyncClient,
    environment: IntegrationEnvironment,
    email_sender: RecordingEmailSender,
    identity_provider: IdentityProviderStub,
) -> None:
    started = await api.post("/v1/auth/email/challenges", json={"email": "dung@example.com"})
    _, link_token = await deliver_pending_email(
        environment, email_sender, started.json()["challenge_id"]
    )
    by_link = await api.post("/v1/auth/email/verify", json={"link_token": link_token})
    assert by_link.status_code == 200
    account = signed_in_from(by_link.json())
    again = await api.post("/v1/auth/email/verify", json={"link_token": link_token})
    assert again.status_code == 401

    # A Google identity with the same verified email is not attached automatically.
    google_token = identity_provider.id_token(subject="dung-google", email="dung@example.com")
    refused = await api.post("/v1/auth/google", json={"id_token": google_token})
    assert refused.status_code == 409
    assert refused.json()["code"] == "ACCOUNT_LINK_REQUIRED"

    # The account holder links it from a recently authenticated session.
    admin_execute(
        environment, "UPDATE iam.sessions SET authenticated_at = now() - interval '1 hour'"
    )
    stale = await api.post(
        "/v1/auth/google", json={"id_token": google_token}, headers=account.headers
    )
    assert stale.json()["code"] == "STEP_UP_REQUIRED"
    admin_execute(environment, "UPDATE iam.sessions SET authenticated_at = now()")
    linked = await api.post(
        "/v1/auth/google", json={"id_token": google_token}, headers=account.headers
    )
    assert linked.status_code == 200
    assert linked.json()["session_id"] == account.session_id
    google = await api.post("/v1/auth/google", json={"id_token": google_token})
    assert google.json()["user"]["id"] == account.user_id


async def test_email_challenges_are_rate_limited_per_address(api: httpx.AsyncClient) -> None:
    statuses = [
        (await api.post("/v1/auth/email/challenges", json={"email": "eve@example.com"})).status_code
        for _ in range(6)
    ]
    assert statuses == [202] * 5 + [429]
    limited = await api.post("/v1/auth/email/challenges", json={"email": "eve@example.com"})
    assert limited.headers["retry-after"].isdigit()
    assert limited.json()["code"] == "RATE_LIMITED"


async def test_step_up_reauthentication_requires_the_same_account(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    environment: IntegrationEnvironment,
) -> None:
    member = await sign_in(api, identity_provider, subject="owner-subject")
    await sign_in(api, identity_provider, subject="someone-else")
    admin_execute(
        environment,
        "UPDATE iam.sessions SET authenticated_at = authenticated_at - interval '2 hours'",
    )
    other_account = identity_provider.id_token(subject="someone-else")
    mismatch = await api.post(
        "/v1/auth/google", json={"id_token": other_account}, headers=member.headers
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "REAUTHENTICATION_MISMATCH"

    same_account = identity_provider.id_token(subject="owner-subject")
    stepped_up = await api.post(
        "/v1/auth/google", json={"id_token": same_account}, headers=member.headers
    )
    assert stepped_up.status_code == 200
    assert stepped_up.json()["refresh_token"] is None
    assert stepped_up.json()["session_id"] == member.session_id
    fresh = admin_fetch(
        environment,
        "SELECT now() - authenticated_at < interval '1 minute' FROM iam.sessions WHERE id = %s",
        member.session_id,
    )
    assert fresh == [(True,)]


async def test_profile_updates_require_current_version(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    member = await sign_in(api, identity_provider)
    missing = await api.patch("/v1/me", json={"display_name": "Lan"}, headers=member.headers)
    assert missing.status_code == 428
    updated = await api.patch(
        "/v1/me",
        json={"display_name": "  Lan   Tran ", "timezone": "Asia/Ho_Chi_Minh", "locale": "vi-VN"},
        headers={**member.headers, "If-Match": '"1"'},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"2"'
    assert updated.json()["display_name"] == "Lan Tran"
    stale = await api.patch(
        "/v1/me", json={"locale": None}, headers={**member.headers, "If-Match": '"1"'}
    )
    assert stale.status_code == 412
    bad_zone = await api.patch(
        "/v1/me", json={"timezone": "Mars/Base"}, headers={**member.headers, "If-Match": '"2"'}
    )
    assert bad_zone.status_code == 422


def test_session_ttl_defaults_cover_offline_window(environment: IntegrationEnvironment) -> None:
    settings = environment.settings
    assert timedelta(seconds=settings.auth_session_idle_ttl_seconds) >= timedelta(days=90)
