"""Transaction-local context consumed by PostgreSQL RLS policies."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_actor_context(session: AsyncSession, actor_id: UUID | str | None) -> None:
    """Set ``app.actor_id``; policies resolve current relationships from it."""

    await session.execute(
        text("SELECT set_config('app.actor_id', :actor_id, true)"),
        {"actor_id": str(actor_id) if actor_id else ""},
    )


async def set_invite_context(session: AsyncSession, token_hash: bytes | None) -> None:
    """Expose the presented invite's digest so policies can scope pre-join reads."""

    await session.execute(
        text("SELECT set_config('app.invite_token_hash', :token_hash, true)"),
        {"token_hash": token_hash.hex() if token_hash else ""},
    )
