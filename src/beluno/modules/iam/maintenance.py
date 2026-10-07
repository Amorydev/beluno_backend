"""Bounded cleanup of short-lived authentication records (worker role only).

Consumed refresh tokens are kept until they expire so reuse can still be
detected. Sessions that ended (revoked or expired) more than 30 days ago go with
their refresh tokens, so device labels do not outlive them; audit rows are never
deleted here.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, text

from beluno.db.models.iam import EmailChallenge, RefreshToken, WebAuthnChallenge
from beluno.modules.context import Runtime

RETENTION_AFTER_EXPIRY = timedelta(days=1)
SESSION_RETENTION_AFTER_END = timedelta(days=30)
PURGE_RATE_LIMIT_WINDOWS = text("DELETE FROM iam.rate_limit_counters WHERE window_start < :cutoff")
PURGE_STALE_SESSIONS = text("SELECT iam.purge_stale_sessions(:cutoff)")


async def purge_expired_auth_records(runtime: Runtime) -> int:
    now = runtime.clock()
    cutoff = now - RETENTION_AFTER_EXPIRY
    removed = 0
    async with runtime.database.transaction() as session:
        for result in (
            await session.execute(delete(EmailChallenge).where(EmailChallenge.expires_at < cutoff)),
            await session.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff)),
            await session.execute(
                delete(WebAuthnChallenge).where(WebAuthnChallenge.expires_at < cutoff)
            ),
            await session.execute(PURGE_RATE_LIMIT_WINDOWS, {"cutoff": cutoff}),
        ):
            removed += result.rowcount  # type: ignore[attr-defined]
        sessions = await session.execute(
            PURGE_STALE_SESSIONS, {"cutoff": now - SESSION_RETENTION_AFTER_END}
        )
        removed += int(sessions.scalar_one())
    return removed
