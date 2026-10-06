"""API-issued access tokens: short-lived ES256 JWTs with key rotation (ADR 0007).

The first configured key signs new tokens; every configured key verifies, so a
retired key can stay listed until its last token expires. Access tokens only
prove identity; current session and relationship state are re-checked in
PostgreSQL on every request.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import HTTPException, status
from jwt import InvalidTokenError

from beluno.config import Settings

ACCESS_TOKEN_ALGORITHM = "ES256"
CLOCK_SKEW_LEEWAY_SECONDS = 10


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    user_id: UUID
    session_id: UUID
    authenticated_at: datetime
    is_guest: bool


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SigningKey:
    kid: str
    private_key: ec.EllipticCurvePrivateKey

    @property
    def public_jwk(self) -> dict[str, str]:
        numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "kid": self.kid,
            "use": "sig",
            "alg": ACCESS_TOKEN_ALGORITHM,
            "x": _b64url_uint(numbers.x),
            "y": _b64url_uint(numbers.y),
        }


def _b64url_uint(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode("ascii")


def load_signing_keys(raw: str) -> tuple[SigningKey, ...]:
    """Parse ``[{"kid": ..., "private_key_pem": ...}, ...]``; the first entry signs."""

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("signing keys must be a JSON array") from error
    if not isinstance(entries, list) or not entries:
        raise ValueError("signing keys must be a non-empty JSON array")
    keys: list[SigningKey] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each signing key must be an object")
        kid = entry.get("kid")
        pem = entry.get("private_key_pem")
        if not isinstance(kid, str) or not kid or not isinstance(pem, str):
            raise ValueError("each signing key needs kid and private_key_pem")
        private_key = load_pem_private_key(pem.encode("utf-8"), password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ValueError("signing keys must be P-256 EC private keys")
        keys.append(SigningKey(kid=kid, private_key=private_key))
    if len({key.kid for key in keys}) != len(keys):
        raise ValueError("signing key ids must be unique")
    return tuple(keys)


def authentication_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication token",
    )


class AccessTokenCodec:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @cached_property
    def _keys(self) -> tuple[SigningKey, ...] | None:
        if self._settings.auth_signing_keys is None:
            return None
        return load_signing_keys(self._settings.auth_signing_keys.get_secret_value())

    @property
    def configured(self) -> bool:
        return self._settings.auth_is_configured and self._keys is not None

    def _require_keys(self) -> tuple[SigningKey, ...]:
        keys = self._keys if self._settings.auth_is_configured else None
        if keys is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is not configured",
            )
        return keys

    def issue(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        authenticated_at: datetime,
        is_guest: bool,
        now: datetime,
    ) -> IssuedAccessToken:
        signing_key = self._require_keys()[0]
        expires_at = now + timedelta(seconds=self._settings.auth_access_token_ttl_seconds)
        claims = {
            "iss": self._settings.auth_issuer,
            "aud": self._settings.auth_audience,
            "sub": str(user_id),
            "sid": str(session_id),
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "auth_time": int(authenticated_at.timestamp()),
            "guest": is_guest,
        }
        token = jwt.encode(
            claims,
            signing_key.private_key,
            algorithm=ACCESS_TOKEN_ALGORITHM,
            headers={"kid": signing_key.kid},
        )
        return IssuedAccessToken(token=token, expires_at=expires_at)

    def verify(self, token: str) -> AuthenticatedActor:
        keys = self._require_keys()
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = next((candidate for candidate in keys if candidate.kid == kid), None)
            if key is None:
                raise authentication_required()
            claims: dict[str, Any] = jwt.decode(
                token,
                key.private_key.public_key(),
                algorithms=[ACCESS_TOKEN_ALGORITHM],
                issuer=self._settings.auth_issuer,
                audience=self._settings.auth_audience,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub", "sid", "auth_time"]},
            )
            return AuthenticatedActor(
                user_id=UUID(str(claims["sub"])),
                session_id=UUID(str(claims["sid"])),
                authenticated_at=datetime.fromtimestamp(int(claims["auth_time"]), tz=UTC),
                is_guest=claims.get("guest") is True,
            )
        except (InvalidTokenError, ValueError, TypeError) as error:
            raise authentication_required() from error

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [key.public_jwk for key in self._require_keys()]}
