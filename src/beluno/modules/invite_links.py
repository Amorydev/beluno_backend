"""Shared mechanics for shareable plan invite links.

Tokens carry 32 random bytes (base64url); only an HMAC digest is stored, and the
raw token is returned once at creation. Unusable invites (unknown, expired,
revoked, exhausted) all produce the same public error; only the category is
logged for abuse monitoring, never the token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from beluno.observability.setup import logger, safe_extra
from beluno.token_hashing import TokenHasher, new_secret_token, normalize_email

INVITE_TOKEN_PURPOSE = "invite_token"
INVITE_EMAIL_PURPOSE = "invite_email"
DEFAULT_TTL_HOURS = 168
MAX_TTL_HOURS = 720

security_log = logger("beluno.security")


class UsableInvite(Protocol):
    state: str
    expires_at: datetime
    max_uses: int | None
    use_count: int


@dataclass(frozen=True)
class IssuedInviteToken:
    raw: str
    digest: bytes


def issue_invite_token(hasher: TokenHasher) -> IssuedInviteToken:
    raw = new_secret_token()
    return IssuedInviteToken(raw=raw, digest=token_digest(hasher, raw))


def token_digest(hasher: TokenHasher, raw: str) -> bytes:
    return hasher.digest(INVITE_TOKEN_PURPOSE, raw)


def email_digest(hasher: TokenHasher, email: str) -> bytes:
    return hasher.digest(INVITE_EMAIL_PURPOSE, normalize_email(email))


def expiry(now: datetime, hours: int) -> datetime:
    return now + timedelta(hours=hours)


def unavailable_reason(invite: UsableInvite, now: datetime) -> str | None:
    if invite.state != "active":
        return "revoked"
    if invite.expires_at <= now:
        return "expired"
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        return "exhausted"
    return None


def log_unavailable(reason: str, operation: str) -> None:
    security_log.info(
        "invite unavailable",
        **safe_extra(event="invite_unavailable", reason=reason, operation=operation),
    )
