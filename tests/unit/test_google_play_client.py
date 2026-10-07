"""The Play Developer API client: the requests it makes and how it reads the answers."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from beluno.config import Settings
from beluno.stores import (
    GOOGLE_CERTS,
    GooglePlayClient,
    PurchaseInvalid,
    PurchasePending,
    StoreProduct,
    StoreUnavailable,
)

PACKAGE = "app.beluno.test"
PASS = "beluno.trip_pass"
PRO = "beluno.pro.yearly"


def settings(**extra: Any) -> Settings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    account = {
        "type": "service_account",
        "client_email": "play@beluno.iam.gserviceaccount.com",
        "private_key": pem,
        "private_key_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        google_play_package_name=PACKAGE,
        google_play_service_account_json=SecretStr(json.dumps(account)),
        store_trip_pass_product_ids=[PASS],
        store_pro_product_ids=[PRO],
        **extra,
    )


def client(answer: Any, seen: list[httpx.Request], **extra: Any) -> GooglePlayClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request) if callable(answer) else answer

    play = GooglePlayClient.from_settings(
        settings(**extra), httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    # A token that is still valid: no call to Google's token endpoint.
    play._credentials.token = "access-token"
    play._credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    return play


async def test_a_trip_pass_is_read_and_consumed() -> None:
    seen: list[httpx.Request] = []
    buyer = uuid4()
    play = client(
        httpx.Response(
            200,
            json={
                "purchaseTimeMillis": "1790000000000",
                "purchaseState": 0,
                "obfuscatedExternalAccountId": str(buyer),
                "purchaseType": 0,
            },
        ),
        seen,
    )
    found = await play.purchase(PASS, "tok/en")
    assert found.product is StoreProduct.TRIP_PASS and found.account == buyer
    assert found.environment == "test" and found.revoked_at is None and found.expires_at is None
    assert (
        seen[0]
        .url.raw_path.decode()
        .endswith(f"/applications/{PACKAGE}/purchases/products/{PASS}/tokens/tok%2Fen")
    )
    assert seen[0].headers["Authorization"] == "Bearer access-token"
    await play.acknowledge(StoreProduct.TRIP_PASS, PASS, "tok/en")
    assert seen[1].method == "POST" and seen[1].url.raw_path.decode().endswith(
        "/tokens/tok%2Fen:consume"
    )


async def test_a_cancelled_pass_is_revoked_and_a_pending_one_waits() -> None:
    cancelled = client(
        httpx.Response(200, json={"purchaseTimeMillis": "1", "purchaseState": 1}), []
    )
    assert (await cancelled.purchase(PASS, "t")).revoked_at is not None
    pending = client(httpx.Response(200, json={"purchaseState": 2}), [])
    with pytest.raises(PurchasePending):
        await pending.purchase(PASS, "t")


async def test_a_subscription_reads_its_latest_expiry_and_is_acknowledged() -> None:
    seen: list[httpx.Request] = []
    play = client(
        httpx.Response(
            200,
            json={
                "startTime": "2026-10-07T10:00:00.123Z",
                "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
                "lineItems": [
                    {"productId": PRO, "expiryTime": "2027-10-07T10:00:00Z"},
                    {"productId": "other", "expiryTime": "2030-01-01T00:00:00Z"},
                ],
            },
        ),
        seen,
    )
    found = await play.purchase(PRO, "token")
    assert found.product is StoreProduct.PRO and found.account is None
    assert found.expires_at == datetime(2027, 10, 7, 10, tzinfo=UTC)
    assert found.environment == "production"
    assert seen[0].url.raw_path.decode().endswith("/purchases/subscriptionsv2/tokens/token")
    await play.acknowledge(StoreProduct.PRO, PRO, "token")
    assert (
        seen[1]
        .url.raw_path.decode()
        .endswith(f"/purchases/subscriptions/{PRO}/tokens/token:acknowledge")
    )


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(404), PurchaseInvalid),
        (httpx.Response(410), PurchaseInvalid),
        (httpx.Response(500), StoreUnavailable),
        (
            httpx.Response(200, json={"startTime": "x", "lineItems": [{"productId": PRO}]}),
            PurchaseInvalid,
        ),
        (httpx.Response(200, json={"lineItems": []}), PurchaseInvalid),
        (
            httpx.Response(200, json={"subscriptionState": "SUBSCRIPTION_STATE_PENDING"}),
            PurchasePending,
        ),
        (
            httpx.Response(
                200,
                json={
                    "startTime": "2026-10-07T10:00:00Z",
                    "lineItems": [{"productId": PRO}],
                    "externalAccountIdentifiers": {"obfuscatedExternalAccountId": "not-ours"},
                },
            ),
            PurchaseInvalid,
        ),
    ],
)
async def test_unknown_or_unreadable_answers(
    answer: httpx.Response, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await client(answer, []).purchase(PRO, "token")


async def test_network_failures_and_unknown_products() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(StoreUnavailable):
        await client(down, []).purchase(PASS, "token")
    with pytest.raises(PurchaseInvalid):
        await client(httpx.Response(200), []).purchase("not.ours", "token")


async def test_pushes_need_the_configured_audience_and_a_bearer_token() -> None:
    unconfigured = client(httpx.Response(200), [])
    assert await unconfigured.push_is_authentic("Bearer x") is False
    configured = client(
        httpx.Response(200),
        [],
        google_play_push_audience="https://api.beluno.test/v1/store-notifications/google",
        google_play_push_service_account="pubsub@beluno.iam.gserviceaccount.com",
    )
    for header in (None, "", "Basic abc", "Bearer "):
        assert await configured.push_is_authentic(header) is False


def test_not_configured() -> None:
    with pytest.raises(StoreUnavailable):
        GooglePlayClient.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]


AUDIENCE = "https://api.beluno.test/v1/store-notifications/google"
PUSHER = "pubsub@beluno.iam.gserviceaccount.com"


async def test_pushes_are_verified_with_googles_keys_fetched_sparingly() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    keys = {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}

    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GOOGLE_CERTS
        return httpx.Response(200, json=keys)

    seen: list[httpx.Request] = []
    play = client(
        answer,
        seen,
        google_play_push_audience=AUDIENCE,
        google_play_push_service_account=PUSHER,
    )

    def token(kid: str = "k1", **claims: Any) -> str:
        body = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "email": PUSHER,
            "email_verified": True,
            "exp": int(time.time()) + 600,
            **claims,
        }
        return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})

    assert await play.push_is_authentic(f"Bearer {token()}") is True
    assert await play.push_is_authentic(f"Bearer {token(aud='https://other')}") is False
    assert await play.push_is_authentic(f"Bearer {token(email='x@evil.test')}") is False
    assert await play.push_is_authentic(f"Bearer {token(exp=int(time.time()) - 10)}") is False
    assert await play.push_is_authentic("Bearer not-a-token") is False
    # Unknown keys do not send us back to Google within the refresh window.
    for _ in range(3):
        assert await play.push_is_authentic(f"Bearer {token(kid='unknown')}") is False
    assert len(seen) == 1


async def test_unreadable_answers_and_missing_tokens_are_the_store_being_unavailable() -> None:
    with pytest.raises(StoreUnavailable):
        await client(httpx.Response(200, content=b"<html>"), []).purchase(PASS, "t")
    with pytest.raises(StoreUnavailable):
        await client(httpx.Response(200, json=[1]), []).purchase(PASS, "t")
    expired = client(httpx.Response(200), [])
    expired._credentials.token = None
    expired._credentials.expiry = None
    # Refreshing reaches Google's token endpoint over plain requests; here it cannot.
    expired._credentials._token_uri = "http://127.0.0.1:9/token"
    with pytest.raises(StoreUnavailable):
        await expired.purchase(PASS, "t")


async def test_a_pass_for_another_product_and_a_replaced_subscription() -> None:
    other = client(
        httpx.Response(
            200, json={"purchaseTimeMillis": "1", "purchaseState": 0, "productId": "else"}
        ),
        [],
    )
    with pytest.raises(PurchaseInvalid):
        await other.purchase(PASS, "t")
    upgraded = client(
        httpx.Response(
            200,
            json={
                "startTime": "2026-10-07T10:00:00Z",
                "lineItems": [{"productId": PRO, "expiryTime": "2027-10-07T10:00:00Z"}],
                "linkedPurchaseToken": "old-token",
            },
        ),
        [],
    )
    assert (await upgraded.purchase(PRO, "new-token")).replaces == "old-token"
