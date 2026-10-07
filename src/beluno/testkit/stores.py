"""Stand-ins for the stores in tests.

``AppleSigner`` is a certificate chain shaped like Apple's (root, intermediate with
Apple's WWDR marker, leaf with the App Store receipt-signing marker) that signs
transactions and notifications as real JWS, so the server's verification runs the
real library against it; only the root differs. ``FakeGooglePlay`` answers what the
Play Developer API would and records confirmations.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from beluno.db.ids import new_id
from beluno.stores import (
    GOOGLE,
    PurchaseInvalid,
    PurchasePending,
    StoreProduct,
    VerifiedPurchase,
)
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan

BUNDLE_ID = "app.beluno.test"
TRIP_PASS_ID = "beluno.trip_pass"
PRO_ID = "beluno.pro.yearly"
PACKAGE = "app.beluno.test"
PUSH_TOKEN = "pubsub-push-token-for-tests"
_WWDR = ObjectIdentifier("1.2.840.113635.100.6.2.1")
_RECEIPT_SIGNING = ObjectIdentifier("1.2.840.113635.100.6.11.1")


def _certificate(
    name: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: x509.Name | None,
    signer: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
    marker: ObjectIdentifier | None,
) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer or subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False
        )
    )
    if marker is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(marker, b"\x05\x00"), critical=False
        )
    return builder.sign(signer, hashes.SHA256())


class AppleSigner:
    def __init__(self) -> None:
        root_key = ec.generate_private_key(ec.SECP256R1())
        intermediate_key = ec.generate_private_key(ec.SECP256R1())
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._root = _certificate("Test Root", root_key, None, root_key, ca=True, marker=None)
        self._intermediate = _certificate(
            "Test WWDR", intermediate_key, self._root.subject, root_key, ca=True, marker=_WWDR
        )
        leaf = _certificate(
            "Test App Store",
            self._leaf_key,
            self._intermediate.subject,
            intermediate_key,
            ca=False,
            marker=_RECEIPT_SIGNING,
        )
        self._chain = [
            base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
            for cert in (leaf, self._intermediate, self._root)
        ]

    def write_root(self, directory: Path) -> str:
        path = directory / "test-apple-root.cer"
        path.write_bytes(self._root.public_bytes(serialization.Encoding.DER))
        return str(path)

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            {"signedDate": int(time.time() * 1000), **claims},
            self._leaf_key,
            algorithm="ES256",
            headers={"x5c": self._chain},
        )

    def transaction(self, *, product_id: str = TRIP_PASS_ID, **claims: Any) -> str:
        now = int(time.time() * 1000)
        original = claims.pop("originalTransactionId", str(uuid4().int)[:16])
        body: dict[str, Any] = {
            "bundleId": BUNDLE_ID,
            "environment": "Sandbox",
            "productId": product_id,
            "originalTransactionId": original,
            "transactionId": original,
            "purchaseDate": now,
            "type": "Consumable" if product_id == TRIP_PASS_ID else "Auto-Renewable Subscription",
        }
        if product_id == PRO_ID:
            body["expiresDate"] = now + 365 * 86_400_000
        return self.sign({**body, **claims})

    def notification(self, signed_transaction: str, notification_type: str = "DID_RENEW") -> str:
        return self.sign(
            {
                "notificationType": notification_type,
                "notificationUUID": str(uuid4()),
                "version": "2.0",
                "data": {
                    "bundleId": BUNDLE_ID,
                    "environment": "Sandbox",
                    "signedTransactionInfo": signed_transaction,
                },
            }
        )


@dataclass
class FakeGooglePlay:
    """Purchase tokens Google would know, and the confirmations it received."""

    purchases: dict[str, VerifiedPurchase | Exception] = field(default_factory=dict)
    acknowledged: list[tuple[StoreProduct, str, str]] = field(default_factory=list)
    # Google's answer when the app already consumed or acknowledged a purchase itself.
    refuse_acknowledgement: bool = False

    def sell(
        self,
        product: StoreProduct,
        account: Any,
        *,
        token: str | None = None,
        expires_at: datetime | None = None,
        replaces: str | None = None,
    ) -> str:
        token = token or f"google-token-{uuid4().hex}"
        now = datetime.now(UTC)
        self.purchases[token] = VerifiedPurchase(
            store=GOOGLE,
            product=product,
            product_id=TRIP_PASS_ID if product is StoreProduct.TRIP_PASS else PRO_ID,
            original_id=token,
            account=account,
            environment="test",
            purchased_at=now,
            expires_at=(expires_at or now + timedelta(days=365))
            if product is StoreProduct.PRO
            else None,
            revoked_at=None,
            replaces=replaces,
        )
        return token

    def pending(self, token: str) -> None:
        self.purchases[token] = PurchasePending()

    async def purchase(self, product_id: str, token: str) -> VerifiedPurchase:
        found = self.purchases.get(token)
        if found is None:
            raise PurchaseInvalid("Google Play does not know this purchase")
        if isinstance(found, Exception):
            raise found
        if found.product_id != product_id:
            raise PurchaseInvalid("the subscription is not for this product")
        return found

    async def acknowledge(self, product: StoreProduct, product_id: str, token: str) -> None:
        if self.refuse_acknowledgement:
            raise PurchaseInvalid("Google Play does not know this purchase")
        self.acknowledged.append((product, product_id, token))

    async def push_is_authentic(self, authorization: str | None) -> bool:
        return authorization == f"Bearer {PUSH_TOKEN}"


def pass_for(admin: AdminDatabase, trip: FinancePlan) -> None:
    """A Trip Pass on the trip, as the store verification would record it."""

    admin.execute(
        "INSERT INTO billing.purchases (id, user_id, store, product, product_id, original_id,"
        " plan_id, environment, purchased_at, created_at, updated_at) VALUES"
        " (%s, %s, 'apple', 'trip_pass', %s, %s, %s, 'Sandbox', now(), now(), now())",
        str(new_id()),
        trip.owner.user_id,
        TRIP_PASS_ID,
        str(new_id()),
        trip.plan_id,
    )
