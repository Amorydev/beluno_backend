"""Synthetic Google/Apple identity provider for tests.

Mints RS256 ID tokens with a locally generated key and resolves that key for the
real ``ExternalIdentityVerifier``, so signature, issuer, audience, expiry, and
nonce checks all run unchanged without network access.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from beluno.config import Settings
from beluno.modules.iam.external_identity import (
    PROVIDERS,
    ExternalIdentityVerifier,
    IdentityProvider,
)

GOOGLE_CLIENT_ID = "test-google-client.apps.googleusercontent.com"
APPLE_CLIENT_ID = "com.beluno.test"


class IdentityProviderStub:
    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)

    def verifier(self, settings: Settings) -> ExternalIdentityVerifier:
        public_key = self._key.public_key()
        return ExternalIdentityVerifier(settings, resolver=lambda _provider, _token: public_key)

    def id_token(
        self,
        provider: IdentityProvider = IdentityProvider.GOOGLE,
        *,
        subject: str | None = None,
        email: str | None = None,
        email_verified: bool = True,
        name: str | None = None,
        nonce: str | None = None,
        audience: str | None = None,
        issued_at: int | None = None,
        lifetime_seconds: int = 3_600,
        signing_key: rsa.RSAPrivateKey | None = None,
    ) -> str:
        now = issued_at or int(time.time())
        claims: dict[str, Any] = {
            "iss": PROVIDERS[provider].issuers[0],
            "aud": audience
            or (GOOGLE_CLIENT_ID if provider is IdentityProvider.GOOGLE else APPLE_CLIENT_ID),
            "sub": subject or f"{provider.value}-{uuid4().hex}",
            "iat": now,
            "exp": now + lifetime_seconds,
        }
        if email is not None:
            claims["email"] = email
            claims["email_verified"] = (
                email_verified
                if provider is IdentityProvider.GOOGLE
                else str(email_verified).lower()
            )
        if name is not None:
            claims["name"] = name
        if nonce is not None:
            claims["nonce"] = nonce
        return jwt.encode(claims, signing_key or self._key, algorithm="RS256")
