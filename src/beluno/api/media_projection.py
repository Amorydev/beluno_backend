"""The ``media`` sync entity: a plan's file records (never URLs or bytes)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from beluno.contracts.media import MediaResponse
from beluno.db.models.media import Media
from beluno.modules.context import CommandContext
from beluno.sync.pull import SnapshotRow
from beluno.sync.scopes import AccessLevel, ScopeKey

MEDIA_TYPES = ("media",)


def media_response(media: Media) -> MediaResponse:
    return MediaResponse(
        id=media.id,
        plan_id=media.plan_id,
        kind=media.kind,  # type: ignore[arg-type]
        state=media.state,  # type: ignore[arg-type]
        rejection=media.rejection,  # type: ignore[arg-type]
        content_type=media.content_type,
        size_bytes=media.size_bytes,
        width=media.width,
        height=media.height,
        expense_id=media.expense_id,
        uploaded_by_user_id=media.uploaded_by_user_id,
        version=media.version,
        created_at=media.created_at,
        updated_at=media.updated_at,
    )


async def load_media(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    media = await ctx.session.get(Media, id)
    if media is None or media.plan_id != scope.scope_id or media.deleted_at is not None:
        return None
    return media_response(media)


async def page_media(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Media).where(Media.plan_id == scope.scope_id, Media.deleted_at.is_(None))
    if after is not None:
        statement = statement.where(Media.id > after)
    rows = (await ctx.session.execute(statement.order_by(Media.id).limit(limit))).scalars()
    return [SnapshotRow(row.id, row.version, media_response(row)) for row in rows]


async def present_media_current(ctx: CommandContext, entity: object) -> BaseModel | None:
    return media_response(entity) if isinstance(entity, Media) else None
