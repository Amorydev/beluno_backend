"""Purchases verified with the App Store and Google Play.

Apple signs transactions and notifications (JWS with an x5c chain to Apple's root
certificates); ``app-store-server-library`` checks the chain, the bundle, and the
environment. Google is asked directly: the Play Developer API tells the state of a
purchase token, and real-time notifications arrive as authenticated Pub/Sub pushes.
Both libraries are synchronous: their work runs in a thread, never inside a database
transaction. The app sets the buyer's user id on every purchase (Apple
``appAccountToken``, Google ``obfuscatedExternalAccountId``) so a purchase cannot be
claimed by another account.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

import anyio
import httpx
import jwt
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.InAppOwnershipType import InAppOwnershipType
from appstoreserverlibrary.models.JWSTransactionDecodedPayload import (
    JWSTransactionDecodedPayload,
)
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
    VerificationStatus,
)
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from beluno.config import Settings

APPLE = "apple"
GOOGLE = "google"
HTTP_TIMEOUT_SECONDS = 10
PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]


class StoreProduct(StrEnum):
    TRIP_PASS = "trip_pass"
    PRO = "pro"


class PurchaseInvalid(Exception):
    """The store does not vouch for this purchase (forged, unknown, or for another app)."""


class PurchasePending(Exception):
    """Paid later (a pending payment method); nothing is unlocked until it completes."""


class StoreUnavailable(Exception):
    """The store is not configured here, or did not answer; trying later may work."""


@dataclass(frozen=True)
class VerifiedPurchase:
    store: str
    product: StoreProduct
    product_id: str
    # The store's lasting id: Apple's original transaction id, Google's purchase token.
    original_id: str
    # The user id the app set on the purchase, when it did.
    account: UUID | None
    environment: str
    purchased_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    # Google: the subscription token this one replaced (an upgrade or resubscription).
    replaces: str | None = None


def product_for(settings: Settings, product_id: str | None) -> StoreProduct:
    if product_id in settings.store_trip_pass_product_ids:
        return StoreProduct.TRIP_PASS
    if product_id in settings.store_pro_product_ids:
        return StoreProduct.PRO
    raise PurchaseInvalid("not a product sold here")


def _from_millis(value: int | str | None) -> datetime | None:
    return datetime.fromtimestamp(int(value) / 1000, UTC) if value is not None else None


def _account(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise PurchaseInvalid("the purchase names no account of ours") from None


# --- Apple ---------------------------------------------------------------------------------


class AppleStore:
    def __init__(self, settings: Settings, verifier: SignedDataVerifier) -> None:
        self._settings = settings
        self._verifier = verifier

    @classmethod
    def from_settings(cls, settings: Settings) -> AppleStore:
        if not settings.apple_bundle_id or not settings.apple_root_certificates:
            raise StoreUnavailable("the App Store is not configured")
        try:
            roots = [Path(path).read_bytes() for path in settings.apple_root_certificates]
        except OSError as error:
            raise StoreUnavailable("Apple's root certificates cannot be read") from error
        verifier = SignedDataVerifier(
            roots,
            settings.apple_online_checks,
            Environment(settings.apple_environment),
            settings.apple_bundle_id,
            settings.apple_app_apple_id,
        )
        return cls(settings, verifier)

    async def transaction(self, signed_transaction: str) -> VerifiedPurchase:
        """A signed transaction from the app (StoreKit 2)."""

        return self._purchase(await self._verified(signed_transaction))

    async def notification(self, signed_payload: str) -> VerifiedPurchase | None:
        """The purchase an App Store Server Notification (v2) is about, if it is ours.

        Only a payload Apple did not sign is refused; notifications about the app itself,
        tests, or products not sold here are ignored (Apple would otherwise retry them).
        """

        try:
            decoded = await anyio.to_thread.run_sync(
                self._verifier.verify_and_decode_notification, signed_payload
            )
        except VerificationException as error:
            raise _verification_error(error) from error
        if decoded.data is None or decoded.data.signedTransactionInfo is None:
            return None
        transaction = await self._verified(decoded.data.signedTransactionInfo)
        try:
            return self._purchase(transaction)
        except PurchaseInvalid:
            return None

    async def _verified(self, signed_transaction: str) -> JWSTransactionDecodedPayload:
        try:
            return await anyio.to_thread.run_sync(
                self._verifier.verify_and_decode_signed_transaction, signed_transaction
            )
        except VerificationException as error:
            raise _verification_error(error) from error

    def _purchase(self, decoded: JWSTransactionDecodedPayload) -> VerifiedPurchase:
        if decoded.originalTransactionId is None or decoded.purchaseDate is None:
            raise PurchaseInvalid("the transaction is incomplete")
        if decoded.inAppOwnershipType == InAppOwnershipType.FAMILY_SHARED:
            raise PurchaseInvalid("purchases shared through Family Sharing are not accepted")
        purchased_at = _from_millis(decoded.purchaseDate)
        assert purchased_at is not None
        product = product_for(self._settings, decoded.productId)
        return VerifiedPurchase(
            store=APPLE,
            product=product,
            product_id=str(decoded.productId),
            original_id=decoded.originalTransactionId,
            account=_account(decoded.appAccountToken),
            environment=self._settings.apple_environment,
            purchased_at=purchased_at,
            expires_at=_from_millis(decoded.expiresDate) if product is StoreProduct.PRO else None,
            revoked_at=_from_millis(decoded.revocationDate),
        )


def _verification_error(error: VerificationException) -> Exception:
    if error.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
        # Apple's certificate status service did not answer.
        return StoreUnavailable("the App Store's certificate check did not answer")
    return PurchaseInvalid("the App Store does not vouch for this transaction")


# --- Google ---------------------------------------------------------------------------------


class GooglePlay(Protocol):
    async def purchase(self, product_id: str, token: str) -> VerifiedPurchase:
        """What Google says about a purchase token."""
        ...

    async def acknowledge(self, product: StoreProduct, product_id: str, token: str) -> None:
        """Confirm a purchase (Google refunds those left unconfirmed for three days)."""
        ...

    async def push_is_authentic(self, authorization: str | None) -> bool:
        """Whether a Pub/Sub push carries Google's token for our subscription."""
        ...


class GooglePlayClient:
    """The Play Developer API over HTTPS with a service account."""

    def __init__(
        self,
        settings: Settings,
        credentials: service_account.Credentials,
        client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._client = client
        self._package = quote(str(settings.google_play_package_name), safe="")
        self._keys = GoogleSigningKeys(client)

    @classmethod
    def from_settings(
        cls, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> GooglePlayClient:
        if (
            settings.google_play_package_name is None
            or settings.google_play_service_account_json is None
        ):
            raise StoreUnavailable("Google Play is not configured")
        info = json.loads(settings.google_play_service_account_json.get_secret_value())
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=[PLAY_SCOPE]
        )
        return cls(settings, credentials, client or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS))

    async def purchase(self, product_id: str, token: str) -> VerifiedPurchase:
        product = product_for(self._settings, product_id)
        if product is StoreProduct.TRIP_PASS:
            body = await self._call(
                "GET",
                f"purchases/products/{_part(product_id)}/tokens/{_part(token)}",
            )
            return self._one_time(product_id, token, body)
        body = await self._call("GET", f"purchases/subscriptionsv2/tokens/{_part(token)}")
        return self._subscription(product_id, token, body)

    async def acknowledge(self, product: StoreProduct, product_id: str, token: str) -> None:
        # A Trip Pass can be bought again for the next trip, so it is consumed.
        kind, action = (
            ("products", "consume")
            if product is StoreProduct.TRIP_PASS
            else ("subscriptions", "acknowledge")
        )
        await self._call(
            "POST",
            f"purchases/{kind}/{_part(product_id)}/tokens/{_part(token)}:{action}",
        )

    async def push_is_authentic(self, authorization: str | None) -> bool:
        audience = self._settings.google_play_push_audience
        sender = self._settings.google_play_push_service_account
        if audience is None or sender is None or not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return False
        key = await self._keys.find(kid) if isinstance(kid, str) else None
        if key is None:
            return False
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=["RS256"], audience=audience, issuer=GOOGLE_ISSUERS
            )
        except jwt.PyJWTError:
            return False
        return claims.get("email") == sender and claims.get("email_verified") is True

    async def _call(self, method: str, path: str) -> dict[str, Any]:
        if not self._credentials.valid:
            try:
                await anyio.to_thread.run_sync(self._credentials.refresh, Request())
            except GoogleAuthError as error:
                raise StoreUnavailable("no access token for Google Play") from error
        try:
            response = await self._client.request(
                method,
                f"{PLAY_API}/{self._package}/{path}",
                headers={"Authorization": f"Bearer {self._credentials.token}"},
            )
        except httpx.HTTPError as error:
            raise StoreUnavailable("Google Play did not answer") from error
        if response.status_code in (400, 404, 410):
            raise PurchaseInvalid("Google Play does not know this purchase")
        if response.status_code >= 400:
            raise StoreUnavailable(f"Google Play answered {response.status_code}")
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as error:
            raise StoreUnavailable("Google Play sent an unreadable answer") from error
        if not isinstance(body, dict):
            raise StoreUnavailable("Google Play sent an unreadable answer")
        return body

    def _one_time(self, product_id: str, token: str, body: dict[str, Any]) -> VerifiedPurchase:
        state = body.get("purchaseState")
        if state == 2:
            raise PurchasePending
        purchased_at = _from_millis(body.get("purchaseTimeMillis"))
        if purchased_at is None or state not in (0, 1):
            raise PurchaseInvalid("the purchase is incomplete")
        if body.get("productId", product_id) != product_id:
            raise PurchaseInvalid("the purchase is for another product")
        return VerifiedPurchase(
            store=GOOGLE,
            product=StoreProduct.TRIP_PASS,
            product_id=product_id,
            original_id=token,
            account=_account(body.get("obfuscatedExternalAccountId")),
            environment="test" if body.get("purchaseType") == 0 else "production",
            purchased_at=purchased_at,
            expires_at=None,
            # Cancelled: refunded or charged back.
            revoked_at=datetime.now(UTC) if state == 1 else None,
        )

    def _subscription(self, product_id: str, token: str, body: dict[str, Any]) -> VerifiedPurchase:
        if body.get("subscriptionState") == "SUBSCRIPTION_STATE_PENDING":
            raise PurchasePending
        items = [item for item in body.get("lineItems", []) if item.get("productId") == product_id]
        if not items or "startTime" not in body:
            raise PurchaseInvalid("the subscription is not for this product")
        expiries = [_rfc3339(item["expiryTime"]) for item in items if item.get("expiryTime")]
        identifiers = body.get("externalAccountIdentifiers") or {}
        return VerifiedPurchase(
            store=GOOGLE,
            product=StoreProduct.PRO,
            product_id=product_id,
            original_id=token,
            account=_account(identifiers.get("obfuscatedExternalAccountId")),
            environment="test" if body.get("testPurchase") is not None else "production",
            purchased_at=_rfc3339(body["startTime"]),
            expires_at=max(expiries) if expiries else None,
            revoked_at=None,
            replaces=body.get("linkedPurchaseToken") or None,
        )


class GoogleSigningKeys:
    """Google's OAuth signing keys, fetched at most once per ``REFRESH`` window.

    The push endpoint is public: a token naming an unknown key never sends us to Google
    more often than that.
    """

    REFRESH = timedelta(minutes=5)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._keys: dict[str, Any] = {}
        self._fetched_at: datetime | None = None
        self._lock = anyio.Lock()

    async def find(self, kid: str) -> Any | None:
        if kid not in self._keys:
            async with self._lock:
                now = datetime.now(UTC)
                if self._fetched_at is None or now - self._fetched_at >= self.REFRESH:
                    self._fetched_at = now
                    self._keys = await self._fetch()
        return self._keys.get(kid)

    async def _fetch(self) -> dict[str, Any]:
        try:
            response = await self._client.get(GOOGLE_CERTS)
            response.raise_for_status()
            found = jwt.PyJWKSet.from_dict(response.json())
        except (httpx.HTTPError, ValueError, jwt.PyJWTError):
            return self._keys  # keep what we had; try again after the window
        return {key.key_id: key.key for key in found.keys if key.key_id}


def _part(value: str) -> str:
    """One path segment of a Play API URL."""

    return quote(value, safe="")


def _rfc3339(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        raise PurchaseInvalid("the store sent an unreadable time") from None
