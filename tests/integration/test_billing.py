"""Trip Pass and Pro bought in the stores, what they unlock, and the free limits."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from beluno.api.main import create_app
from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.billing import acknowledge_purchase
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.stores import StoreProduct
from beluno.testkit.api_client import SignedIn, sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import if_match, join_with_invite
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.stores import (
    BUNDLE_ID,
    PACKAGE,
    PRO_ID,
    PUSH_TOKEN,
    TRIP_PASS_ID,
    AppleSigner,
    FakeGooglePlay,
)
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration


@pytest.fixture
def signer() -> AppleSigner:
    return AppleSigner()


@pytest.fixture
def play() -> FakeGooglePlay:
    return FakeGooglePlay()


@pytest.fixture
def store_settings(live_settings: Settings, signer: AppleSigner, tmp_path: Path) -> Settings:
    return live_settings.model_copy(
        update={
            "free_active_trips": 2,
            "media_receipts_per_plan": 5,
            "store_trip_pass_product_ids": [TRIP_PASS_ID],
            "store_pro_product_ids": [PRO_ID],
            "apple_bundle_id": BUNDLE_ID,
            "apple_root_certificates": [signer.write_root(tmp_path)],
            "apple_online_checks": False,
            "google_play_package_name": PACKAGE,
        }
    )


@pytest.fixture
async def shop(
    store_settings: Settings, identity_provider: IdentityProviderStub, play: FakeGooglePlay
) -> AsyncIterator[httpx.AsyncClient]:
    database = Database(store_settings)
    app = create_app(
        settings=store_settings,
        database=database,
        identity_verifier=identity_provider.verifier(store_settings),
        google_play=play,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await database.close()


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def refused(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text


async def new_plan(
    api: httpx.AsyncClient, user: SignedIn, plan_type: str = "trip"
) -> httpx.Response:
    return await api.post(
        "/v1/plans",
        json={"type": plan_type, "title": "Trip", "base_currency": "USD"},
        headers=user.headers,
    )


async def trip(api: httpx.AsyncClient, user: SignedIn) -> str:
    return str((await ok(await new_plan(api, user), 201))["id"])


async def buy_pass(
    api: httpx.AsyncClient, signer: AppleSigner, user: SignedIn, plan_id: str, **claims: Any
) -> httpx.Response:
    return await api.post(
        "/v1/me/purchases/apple",
        json={
            "signed_transaction": signer.transaction(appAccountToken=user.user_id, **claims),
            "plan_id": plan_id,
        },
        headers=user.headers,
    )


async def unlock(api: httpx.AsyncClient, user: SignedIn, plan_id: str) -> Any:
    return await ok(await api.get(f"/v1/plans/{plan_id}/entitlement", headers=user.headers))


async def test_the_free_limit_counts_your_own_trips_until_a_pass_unlocks_one(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, signer: AppleSigner
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    first, second = await trip(shop, ann), await trip(shop, ann)
    refused(await new_plan(shop, ann), 409, "TRIP_LIMIT_REACHED")
    await ok(await new_plan(shop, ann, "hangout"), 201)  # hangouts are always free
    # Trips of others do not count toward yours.
    bea = await sign_in(shop, identity_provider, name="Bea")
    await join_with_invite(shop, bea, await trip(shop, bea), ann)
    entitled = await ok(await shop.get("/v1/me/entitlements", headers=ann.headers))
    assert entitled["active_trips"] == 2 and entitled["active_trip_limit"] == 2
    assert entitled["trip_pass_product_ids"] == [TRIP_PASS_ID]

    bought = await ok(await buy_pass(shop, signer, ann, first))
    assert bought["product"] == "trip_pass" and bought["plan_id"] == first and bought["active"]
    assert (await unlock(shop, ann, first))["unlocked_by"] == "trip_pass"
    third = await trip(shop, ann)  # the unlocked trip no longer counts

    # Finishing a trip frees a place; reopening it needs one again.
    await ok(
        await shop.post(
            f"/v1/plans/{second}/state", json={"state": "active"}, headers=if_match(1, ann)
        )
    )
    await ok(
        await shop.post(
            f"/v1/plans/{second}/state", json={"state": "completed"}, headers=if_match(2, ann)
        )
    )
    fourth = await trip(shop, ann)
    refused(
        await shop.post(
            f"/v1/plans/{second}/state", json={"state": "settling"}, headers=if_match(3, ann)
        ),
        409,
        "TRIP_LIMIT_REACHED",
    )
    assert {third, fourth}


async def test_two_trips_started_at_once_do_not_pass_the_limit(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    await trip(shop, ann)
    answers = await asyncio.gather(new_plan(shop, ann), new_plan(shop, ann))
    assert sorted(answer.status_code for answer in answers) == [201, 409]


async def test_a_trip_pass_is_bound_to_its_buyer_and_its_trip(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, signer: AppleSigner
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    bea = await sign_in(shop, identity_provider, name="Bea")
    plan_id = await trip(shop, ann)
    await join_with_invite(shop, ann, plan_id, bea)
    other = await trip(shop, ann)
    signed = signer.transaction(appAccountToken=ann.user_id, originalTransactionId="1000")

    def body(plan: str | None, transaction: str = signed) -> dict[str, Any]:
        return {"signed_transaction": transaction, "plan_id": plan}

    first = await ok(
        await shop.post("/v1/me/purchases/apple", json=body(plan_id), headers=ann.headers)
    )
    again = await ok(
        await shop.post("/v1/me/purchases/apple", json=body(plan_id), headers=ann.headers)
    )
    assert again["id"] == first["id"]  # restoring is sending it again
    refused(
        await shop.post("/v1/me/purchases/apple", json=body(other), headers=ann.headers),
        409,
        "PURCHASE_USED",
    )
    # Someone else cannot take it, with or without the buyer's account on it.
    refused(
        await shop.post("/v1/me/purchases/apple", json=body(plan_id), headers=bea.headers),
        409,
        "PURCHASE_OWNED_ELSEWHERE",
    )
    unmarked = signer.transaction(originalTransactionId="1000")
    refused(
        await shop.post(
            "/v1/me/purchases/apple", json=body(plan_id, unmarked), headers=bea.headers
        ),
        409,
        "PURCHASE_OWNED_ELSEWHERE",
    )
    # Everyone on the trip sees it unlocked.
    assert (await unlock(shop, bea, plan_id))["unlocked_by"] == "trip_pass"
    assert (await unlock(shop, bea, plan_id))["receipt_limit"] is None
    assert (await unlock(shop, ann, other))["receipt_limit"] == 5


async def test_purchases_the_store_does_not_vouch_for_are_refused(
    shop: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    signer: AppleSigner,
    play: FakeGooglePlay,
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    plan_id = await trip(shop, ann)
    hangout = (await ok(await new_plan(shop, ann, "hangout"), 201))["id"]
    stranger = await sign_in(shop, identity_provider, name="Sol")
    forged = AppleSigner().transaction(appAccountToken=ann.user_id)
    cases = [
        ({"signed_transaction": forged, "plan_id": plan_id}, ann, 422, "PURCHASE_INVALID"),
        ({"signed_transaction": "not-a-jws", "plan_id": plan_id}, ann, 422, "PURCHASE_INVALID"),
        (
            {
                "signed_transaction": signer.transaction(productId="other.app.product"),
                "plan_id": plan_id,
            },
            ann,
            422,
            "PURCHASE_INVALID",
        ),
        (
            {"signed_transaction": signer.transaction(bundleId="other.app"), "plan_id": plan_id},
            ann,
            422,
            "PURCHASE_INVALID",
        ),
        (
            {
                "signed_transaction": signer.transaction(appAccountToken=stranger.user_id),
                "plan_id": plan_id,
            },
            ann,
            409,
            "PURCHASE_OWNED_ELSEWHERE",
        ),
        (
            {"signed_transaction": signer.transaction(), "plan_id": None},
            ann,
            422,
            "VALIDATION_FAILED",
        ),
        (
            {"signed_transaction": signer.transaction(), "plan_id": hangout},
            ann,
            409,
            "NOT_AVAILABLE_FOR_HANGOUT",
        ),
        (
            {"signed_transaction": signer.transaction(), "plan_id": plan_id},
            stranger,
            404,
            "NOT_FOUND",
        ),
    ]
    for body, user, status, code in cases:
        refused(
            await shop.post("/v1/me/purchases/apple", json=body, headers=user.headers), status, code
        )

    play.pending("pending-token")
    refused(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": "pending-token"},
            headers=ann.headers,
        ),
        409,
        "PURCHASE_PENDING",
    )
    refused(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": "unknown-token"},
            headers=ann.headers,
        ),
        422,
        "PURCHASE_INVALID",
    )


async def test_guests_cannot_buy(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, signer: AppleSigner
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    plan_id = await trip(shop, ann)
    invite = await ok(
        await shop.post(f"/v1/plans/{plan_id}/invites", json={}, headers=ann.headers), 201
    )
    redeemed = await ok(
        await shop.post(
            "/v1/invites/redeem", json={"token": invite["token"], "display_name": "Gia"}
        )
    )
    guest = signed_in_from(redeemed["session"])
    refused(await buy_pass(shop, signer, guest, plan_id), 403, "FORBIDDEN")


async def test_pro_on_google_unlocks_the_owners_trips_and_is_confirmed_once(
    shop: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    play: FakeGooglePlay,
    store_settings: Settings,
    admin: AdminDatabase,
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    bea = await sign_in(shop, identity_provider, name="Bea")
    plans = [await trip(shop, ann), await trip(shop, ann)]
    await join_with_invite(shop, ann, plans[0], bea)
    token = play.sell(StoreProduct.PRO, UUID(ann.user_id))
    bought = await ok(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": token},
            headers=ann.headers,
        )
    )
    assert bought["product"] == "pro" and bought["active"] and bought["expires_at"]
    entitled = await ok(await shop.get("/v1/me/entitlements", headers=ann.headers))
    assert entitled["pro"]["store"] == "google" and entitled["active_trip_limit"] is None
    await trip(shop, ann)  # no limit with Pro
    assert (await unlock(shop, bea, plans[0]))["unlocked_by"] == "pro"
    # Bea sees nothing of Ann's purchase, and her own trips are not Ann's: still limited.
    assert (await ok(await shop.get("/v1/me/entitlements", headers=bea.headers)))["pro"] is None
    assert (await unlock(shop, bea, await trip(shop, bea)))["unlocked_by"] is None

    [(job_args,)] = admin.fetch(
        "SELECT args FROM jobs.procrastinate_jobs WHERE task_name = 'billing.acknowledge_purchase'"
    )
    worker = await worker_runtime(store_settings)
    try:
        purchase_id = UUID(job_args["purchase_id"])
        assert await acknowledge_purchase(worker, play, purchase_id) is True
        assert await acknowledge_purchase(worker, play, purchase_id) is False
    finally:
        await worker.database.close()
    assert play.acknowledged == [(StoreProduct.PRO, PRO_ID, token)]


async def worker_runtime(settings: Settings) -> Runtime:
    return Runtime(
        settings=settings,
        database=Database.for_worker(settings),
        tokens=AccessTokenCodec(settings),
        hasher=TokenHasher.from_settings(settings),
        identity_verifier=ExternalIdentityVerifier(settings),
    )


async def test_app_store_notifications_renew_and_refund(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, signer: AppleSigner
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    plan_id = await trip(shop, ann)
    await ok(await buy_pass(shop, signer, ann, plan_id, originalTransactionId="2000"))
    now = int(time.time() * 1000)
    pro = signer.transaction(
        product_id=PRO_ID,
        appAccountToken=ann.user_id,
        originalTransactionId="3000",
        expiresDate=now + 60_000,
    )
    await ok(
        await shop.post(
            "/v1/me/purchases/apple", json={"signed_transaction": pro}, headers=ann.headers
        )
    )

    async def notify(transaction: str, kind: str) -> None:
        await ok(
            await shop.post(
                "/v1/store-notifications/apple",
                json={"signedPayload": signer.notification(transaction, kind)},
            ),
            204,
        )

    renewed = now + 400 * 86_400_000
    await notify(
        signer.transaction(product_id=PRO_ID, originalTransactionId="3000", expiresDate=renewed),
        "DID_RENEW",
    )
    entitled = await ok(await shop.get("/v1/me/entitlements", headers=ann.headers))
    assert datetime.fromisoformat(entitled["pro"]["expires_at"]) == datetime.fromtimestamp(
        renewed / 1000, UTC
    )

    await notify(signer.transaction(originalTransactionId="2000", revocationDate=now), "REFUND")
    found = await unlock(shop, ann, plan_id)
    assert found["unlocked_by"] == "pro"  # the pass is gone; Ann's Pro still covers it

    def period(**claims: Any) -> str:
        return signer.transaction(
            product_id=PRO_ID, originalTransactionId="3000", expiresDate=renewed, **claims
        )

    async def pro_active() -> bool:
        found = await ok(await shop.get("/v1/me/entitlements", headers=ann.headers))
        return found["pro"] is not None

    # The current period refunded: Pro ends. Sending the pre-refund copy again does not
    # bring it back; Apple reversing the refund does.
    await notify(period(revocationDate=now), "REFUND")
    assert (await unlock(shop, ann, plan_id))["unlocked_by"] is None
    replayed = await ok(
        await shop.post(
            "/v1/me/purchases/apple", json={"signed_transaction": period()}, headers=ann.headers
        )
    )
    assert replayed["active"] is False and not await pro_active()
    await notify(period(), "REFUND_REVERSED")
    assert await pro_active()
    # A refunded older period changes nothing; a later paid period outlasts a refund.
    await notify(
        signer.transaction(
            product_id=PRO_ID,
            originalTransactionId="3000",
            expiresDate=now + 1_000,
            revocationDate=now,
        ),
        "REFUND",
    )
    assert await pro_active()
    await notify(period(revocationDate=now), "REFUND")
    await notify(
        signer.transaction(
            product_id=PRO_ID, originalTransactionId="3000", expiresDate=renewed + 86_400_000
        ),
        "DID_RENEW",
    )
    assert await pro_active()

    # Notifications about products not sold here are acknowledged and ignored.
    await notify(signer.transaction(productId="other.product"), "ONE_TIME_CHARGE")
    await notify(signer.transaction(inAppOwnershipType="FAMILY_SHARED"), "DID_RENEW")

    forged = AppleSigner()
    refused(
        await shop.post(
            "/v1/store-notifications/apple",
            json={"signedPayload": forged.notification(forged.transaction(), "REFUND")},
        ),
        422,
        "PURCHASE_INVALID",
    )


def push(payload: dict[str, Any]) -> dict[str, Any]:
    data = base64.b64encode(json.dumps({"packageName": PACKAGE, **payload}).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "projects/x/subs/y"}


async def test_google_notifications_renew_revoke_and_void(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, play: FakeGooglePlay
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    plan_id = await trip(shop, ann)
    pass_token = play.sell(StoreProduct.TRIP_PASS, UUID(ann.user_id))
    await ok(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": TRIP_PASS_ID, "purchase_token": pass_token, "plan_id": plan_id},
            headers=ann.headers,
        )
    )
    soon = datetime.now(UTC) + timedelta(minutes=5)
    pro_token = play.sell(StoreProduct.PRO, UUID(ann.user_id), expires_at=soon)
    await ok(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": pro_token},
            headers=ann.headers,
        )
    )
    headers = {"Authorization": f"Bearer {PUSH_TOKEN}"}

    async def notify(payload: dict[str, Any]) -> None:
        await ok(
            await shop.post("/v1/store-notifications/google", json=push(payload), headers=headers),
            204,
        )

    refused(
        await shop.post(
            "/v1/store-notifications/google",
            json=push({"testNotification": {}}),
            headers={"Authorization": "Bearer forged"},
        ),
        401,
        "AUTHENTICATION_REQUIRED",
    )
    await notify({"testNotification": {"version": "1.0"}})

    # A renewal: Google now reports a later expiry.
    later = datetime.now(UTC) + timedelta(days=365)
    play.sell(StoreProduct.PRO, UUID(ann.user_id), token=pro_token, expires_at=later)
    subscription = {"purchaseToken": pro_token, "subscriptionId": PRO_ID}
    await notify({"subscriptionNotification": {**subscription, "notificationType": 2}})
    entitled = await ok(await shop.get("/v1/me/entitlements", headers=ann.headers))
    assert datetime.fromisoformat(entitled["pro"]["expires_at"]) == later

    # Another app's notification changes nothing.
    voided = {"purchaseToken": pass_token, "productType": 2, "refundType": 1}
    other = push({"voidedPurchaseNotification": voided})
    other["message"]["data"] = base64.b64encode(
        json.dumps(
            {
                "packageName": "other.app",
                "voidedPurchaseNotification": voided,
            }
        ).encode()
    ).decode()
    await ok(await shop.post("/v1/store-notifications/google", json=other, headers=headers), 204)
    assert (await unlock(shop, ann, plan_id))["unlocked_by"] == "trip_pass"

    # One refunded renewal does not end a subscription by itself.
    await notify({"voidedPurchaseNotification": {"purchaseToken": pro_token, "productType": 1}})
    assert (await ok(await shop.get("/v1/me/entitlements", headers=ann.headers)))["pro"]
    await notify({"voidedPurchaseNotification": voided})
    await notify({"subscriptionNotification": {**subscription, "notificationType": 12}})
    assert (await unlock(shop, ann, plan_id))["unlocked_by"] is None
    assert (await ok(await shop.get("/v1/me/entitlements", headers=ann.headers)))["pro"] is None


async def test_stores_not_configured_answer_unavailable(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    ann = await sign_in(api, identity_provider, name="Ann")
    refused(
        await api.post(
            "/v1/me/purchases/apple", json={"signed_transaction": "x"}, headers=ann.headers
        ),
        503,
        "STORE_UNAVAILABLE",
    )
    refused(
        await api.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": "t"},
            headers=ann.headers,
        ),
        503,
        "STORE_UNAVAILABLE",
    )
    # Without limits set, nothing is limited.
    entitled = await ok(await api.get("/v1/me/entitlements", headers=ann.headers))
    assert entitled["active_trip_limit"] is None and entitled["receipts_per_trip"] is None
    for _ in range(3):
        await trip(api, ann)


async def plan_version(api: httpx.AsyncClient, user: SignedIn, plan_id: str) -> int:
    return int((await ok(await api.get(f"/v1/plans/{plan_id}", headers=user.headers)))["version"])


async def test_restoring_or_reopening_a_trip_takes_a_place_with_its_owner(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    bea = await sign_in(shop, identity_provider, name="Bea")
    deleted, kept = await trip(shop, ann), await trip(shop, ann)
    await ok(
        await shop.delete(
            f"/v1/plans/{deleted}", headers=if_match(await plan_version(shop, ann, deleted), ann)
        )
    )
    await trip(shop, ann)  # the deleted trip no longer counts
    refused(
        await shop.post(
            f"/v1/plans/{deleted}/restore",
            headers=if_match(await plan_version(shop, ann, deleted), ann),
        ),
        409,
        "TRIP_LIMIT_REACHED",
    )

    # An admin reopening the owner's trip takes the owner's place too.
    joined = await join_with_invite(shop, ann, kept, bea)
    await ok(
        await shop.patch(
            f"/v1/plans/{kept}/participants/{joined['id']}",
            json={"role": "admin"},
            headers=if_match(joined["version"], ann),
        )
    )
    await ok(
        await shop.post(
            f"/v1/plans/{kept}/state",
            json={"state": "cancelled"},
            headers=if_match(await plan_version(shop, ann, kept), ann),
        )
    )
    await trip(shop, ann)
    refused(
        await shop.post(
            f"/v1/plans/{kept}/state",
            json={"state": "planning"},
            headers=if_match(await plan_version(shop, bea, kept), bea),
        ),
        409,
        "TRIP_LIMIT_REACHED",
    )


async def test_handing_over_a_trip_needs_a_place_with_the_new_owner(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, signer: AppleSigner
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    bea = await sign_in(shop, identity_provider, name="Bea")
    plan_id = await trip(shop, ann)
    joined = await join_with_invite(shop, ann, plan_id, bea)
    for _ in range(2):
        await trip(shop, bea)

    async def hand_over() -> httpx.Response:
        return await shop.post(
            f"/v1/plans/{plan_id}/ownership-transfer",
            json={"new_owner_participant_id": joined["id"]},
            headers=if_match(await plan_version(shop, ann, plan_id), ann),
        )

    refused(await hand_over(), 409, "TRIP_LIMIT_REACHED")
    await ok(await buy_pass(shop, signer, ann, plan_id))
    await ok(await hand_over())


async def test_a_purchase_without_an_account_goes_to_whoever_records_it_first(
    shop: httpx.AsyncClient, identity_provider: IdentityProviderStub, signer: AppleSigner
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    bea = await sign_in(shop, identity_provider, name="Bea")
    pro = signer.transaction(product_id=PRO_ID, originalTransactionId="5000")
    body = {"signed_transaction": pro}
    await ok(await shop.post("/v1/me/purchases/apple", json=body, headers=ann.headers))
    refused(
        await shop.post("/v1/me/purchases/apple", json=body, headers=bea.headers),
        409,
        "PURCHASE_OWNED_ELSEWHERE",
    )
    shared = signer.transaction(
        product_id=PRO_ID, appAccountToken=bea.user_id, inAppOwnershipType="FAMILY_SHARED"
    )
    refused(
        await shop.post(
            "/v1/me/purchases/apple", json={"signed_transaction": shared}, headers=bea.headers
        ),
        422,
        "PURCHASE_INVALID",
    )
    production = signer.transaction(product_id=PRO_ID, environment="Production")
    refused(
        await shop.post(
            "/v1/me/purchases/apple", json={"signed_transaction": production}, headers=bea.headers
        ),
        422,
        "PURCHASE_INVALID",
    )


async def test_a_replaced_google_subscription_stops_counting(
    shop: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    play: FakeGooglePlay,
    store_settings: Settings,
    admin: AdminDatabase,
) -> None:
    ann = await sign_in(shop, identity_provider, name="Ann")
    bea = await sign_in(shop, identity_provider, name="Bea")
    old = play.sell(StoreProduct.PRO, UUID(ann.user_id))
    await ok(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": old},
            headers=ann.headers,
        )
    )
    # Resubscribed from the Play Store while signed in to another account.
    new = play.sell(StoreProduct.PRO, UUID(bea.user_id), replaces=old)
    await ok(
        await shop.post(
            "/v1/me/purchases/google",
            json={"product_id": PRO_ID, "purchase_token": new},
            headers=bea.headers,
        )
    )
    assert (await ok(await shop.get("/v1/me/entitlements", headers=ann.headers)))["pro"] is None
    assert (await ok(await shop.get("/v1/me/entitlements", headers=bea.headers)))["pro"]

    # A purchase the app consumed itself is still marked confirmed.
    play.refuse_acknowledgement = True
    worker = await worker_runtime(store_settings)
    try:
        [(purchase_id,)] = admin.fetch(
            "SELECT id FROM billing.purchases WHERE original_id = %s", new
        )
        assert await acknowledge_purchase(worker, play, purchase_id) is True
        assert await acknowledge_purchase(worker, play, purchase_id) is False
    finally:
        await worker.database.close()
