"""Verify Google and Apple ID tokens presented by mobile/web clients.

The client completes the provider's native sign-in and sends the ID token; the
API checks signature (provider JWKS), issuer, audience (our client IDs), expiry,
and the optional client nonce before trusting the subject or email.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientConnectionError, PyJWKClientError

from beluno.config import Settings
from beluno.contracts.errors import authentication_failed, authentication_unavailable
from beluno.token_hashing import normalize_email


class IdentityProvider(StrEnum):
    GOOGLE = "google"
    APPLE = "apple"
    EMAIL = "email"
    # A passkey already belongs to an account: its subject is that account's user ID.
    PASSKEY = "passkey"


@dataclass(frozen=True)
class VerifiedIdentity:
    """A proven identity: an external subject or an email address the caller controls."""

    provider: IdentityProvider
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None = None


@dataclass(frozen=True)
class ProviderMetadata:
    issuers: tuple[str, ...]
    jwks_url: str


PROVIDERS: dict[IdentityProvider, ProviderMetadata] = {
    IdentityProvider.GOOGLE: ProviderMetadata(
        issuers=("https://accounts.google.com", "accounts.google.com"),
        jwks_url="https://www.googleapis.com/oauth2/v3/certs",
    ),
    IdentityProvider.APPLE: ProviderMetadata(
        issuers=("https://appleid.apple.com",),
        jwks_url="https://appleid.apple.com/auth/keys",
    ),
}

# Resolves the verification key for (provider, raw ID token); blocking I/O allowed.
SigningKeyResolver = Callable[[IdentityProvider, str], Any]


def _default_resolver() -> SigningKeyResolver:
    clients = {
        provider: PyJWKClient(metadata.jwks_url, cache_keys=True, lifespan=3_600, timeout=5)
        for provider, metadata in PROVIDERS.items()
    }

    def resolve(provider: IdentityProvider, token: str) -> Any:
        return clients[provider].get_signing_key_from_jwt(token).key

    return resolve


class ExternalIdentityVerifier:
    def __init__(self, settings: Settings, resolver: SigningKeyResolver | None = None) -> None:
        self._settings = settings
        self._resolver = resolver

    def _audiences(self, provider: IdentityProvider) -> list[str]:
        if provider is IdentityProvider.GOOGLE:
            return list(self._settings.auth_google_client_ids)
        return list(self._settings.auth_apple_client_ids)

    async def verify(
        self,
        provider: IdentityProvider,
        id_token: str,
        *,
        nonce: str | None,
        display_name: str | None = None,
    ) -> VerifiedIdentity:
        audiences = self._audiences(provider)
        if not audiences:
            raise authentication_unavailable("This sign-in provider is not configured")
        if self._resolver is None:
            self._resolver = _default_resolver()
        resolver = self._resolver
        try:
            key = await asyncio.to_thread(resolver, provider, id_token)
        except PyJWKClientConnectionError as error:
            raise authentication_unavailable("Sign-in provider keys are unavailable") from error
        except (PyJWKClientError, InvalidTokenError, ValueError) as error:
            raise authentication_failed() from error
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key,
                algorithms=["RS256", "ES256"],
                audience=audiences,
                issuer=PROVIDERS[provider].issuers,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except InvalidTokenError as error:
            raise authentication_failed() from error
        if not _nonce_matches(claims.get("nonce"), nonce):
            raise authentication_failed()
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise authentication_failed()
        email = claims.get("email")
        email_verified = claims.get("email_verified") in (True, "true")
        name = claims.get("name") if provider is IdentityProvider.GOOGLE else None
        return VerifiedIdentity(
            provider=provider,
            subject=subject,
            email=normalize_email(email) if isinstance(email, str) and email else None,
            email_verified=email_verified and isinstance(email, str),
            display_name=_clean_name(name) or _clean_name(display_name),
        )


def _nonce_matches(claimed: object, presented: str | None) -> bool:
    """Apple embeds SHA-256(nonce); Google embeds the nonce as sent."""

    if presented is None:
        return claimed is None
    if not isinstance(claimed, str):
        return False
    hashed = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(claimed, presented) or hmac.compare_digest(claimed, hashed)


def _clean_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:80]
    return cleaned or None
