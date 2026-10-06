"""Bounded cleanup of short-lived authentication records (worker role only).

Consumed refresh tokens are kept until they expire so reuse can still be
detected; sessions and audit rows are never deleted here.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, text

from beluno.db.models.iam import EmailChallenge, RefreshToken
from beluno.modules.context import Runtime

RETENTION_AFTER_EXPIRY = timedelta(days=1)
PURGE_RATE_LIMIT_WINDOWS = text("DELETE FROM iam.rate_limit_counters WHERE window_start < :cutoff")


async def purge_expired_auth_records(runtime: Runtime) -> int:
    cutoff = runtime.clock() - RETENTION_AFTER_EXPIRY
    removed = 0
    async with runtime.database.transaction() as session:
        for result in (
            await session.execute(delete(EmailChallenge).where(EmailChallenge.expires_at < cutoff)),
            await session.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff)),
            await session.execute(PURGE_RATE_LIMIT_WINDOWS, {"cutoff": cutoff}),
        ):
            removed += result.rowcount  # type: ignore[attr-defined]
    return removed
