"""Keyed hashing and random generation for bearer secrets.

Invite tokens, refresh tokens, email codes, and rate-limit subjects are stored
only as HMAC-SHA256 digests. The purpose string separates domains so a digest
from one context can never satisfy a lookup in another.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from beluno.config import Settings


class TokenHasher:
    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenHasher | None:
        if settings.token_hash_key is None:
            return None
        return cls(settings.token_hash_key.get_secret_value())

    def digest(self, purpose: str, value: str) -> bytes:
        message = f"{purpose}\x00{value}".encode()
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def matches(self, purpose: str, value: str, expected: bytes) -> bool:
        return hmac.compare_digest(self.digest(purpose, value), expected)


def new_secret_token(num_bytes: int = 32) -> str:
    """Return a URL-safe token with ``num_bytes`` of entropy and no padding."""

    return base64.urlsafe_b64encode(secrets.token_bytes(num_bytes)).rstrip(b"=").decode("ascii")


def new_numeric_code(digits: int = 6) -> str:
    return f"{secrets.randbelow(10**digits):0{digits}d}"


def normalize_email(email: str) -> str:
    return email.strip().lower()
