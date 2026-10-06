"""PostgreSQL-backed fixed-window abuse limits (no Redis per ADR 0001).

Each hit commits in its own short transaction so failed attempts still count when
the guarded request later rolls back. Buckets are keyed by an HMAC of
``scope|subject``; raw emails and IP addresses are never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

from beluno.contracts.errors import rate_limited
from beluno.modules.context import Runtime
from beluno.observability.setup import logger, safe_extra

security_log = logger("beluno.security")


@dataclass(frozen=True)
class RateLimit:
    scope: str
    limit: int
    window_seconds: int


EMAIL_CHALLENGE_PER_EMAIL = RateLimit("email_challenge:email", 5, 3_600)
EMAIL_CHALLENGE_PER_CLIENT = RateLimit("email_challenge:client", 20, 3_600)
EMAIL_VERIFY_PER_CLIENT = RateLimit("email_verify:client", 30, 600)
EXTERNAL_SIGN_IN_PER_CLIENT = RateLimit("external_sign_in:client", 60, 600)
REFRESH_PER_CLIENT = RateLimit("refresh:client", 120, 600)
INVITE_PREVIEW_PER_CLIENT = RateLimit("invite_preview:client", 60, 600)
INVITE_REDEEM_PER_CLIENT = RateLimit("invite_redeem:client", 30, 600)
GUEST_CREATION_PER_CLIENT = RateLimit("guest_creation:client", 10, 3_600)
INVITE_CREATION_PER_USER = RateLimit("invite_creation:user", 50, 3_600)
MEMBERSHIP_CHANGES_PER_USER = RateLimit("membership_change:user", 120, 3_600)
SERIES_CREATION_PER_USER = RateLimit("series_creation:user", 20, 3_600)
DUPLICATION_PER_USER = RateLimit("plan_duplication:user", 30, 3_600)
SYNC_PUSH_PER_USER = RateLimit("sync_push:user", 120, 600)
FINANCE_WRITES_PER_PLAN = RateLimit("finance_write:user_plan", 120, 60)

INCREMENT_SQL = text(
    """
    INSERT INTO iam.rate_limit_counters (bucket, window_start, hits)
    VALUES (:bucket, :window_start, 1)
    ON CONFLICT (bucket, window_start)
    DO UPDATE SET hits = iam.rate_limit_counters.hits + 1
    RETURNING hits
    """
)


def window_start(now: datetime, window_seconds: int) -> datetime:
    epoch_seconds = int(now.timestamp())
    return now - timedelta(seconds=epoch_seconds % window_seconds, microseconds=now.microsecond)


async def enforce_rate_limit(runtime: Runtime, limit: RateLimit, subject: str) -> None:
    """Count one hit for ``subject``; raise ``RATE_LIMITED`` once the window is full."""

    hasher = runtime.require_hasher()
    now = runtime.clock()
    start = window_start(now, limit.window_seconds)
    async with runtime.database.transaction() as session:
        hits = (
            await session.execute(
                INCREMENT_SQL,
                {"bucket": hasher.digest(f"rate:{limit.scope}", subject), "window_start": start},
            )
        ).scalar_one()
    if hits > limit.limit:
        security_log.warning(
            "rate limit exceeded",
            **safe_extra(event="rate_limited", scope=limit.scope),
        )
        retry_after = int((start + timedelta(seconds=limit.window_seconds) - now).total_seconds())
        raise rate_limited(retry_after)
