"""Media commands that work offline: record, describe, highlight, or delete a file."""

from __future__ import annotations

from typing import Any

from beluno.api.media_projection import media_response
from beluno.contracts.media import (
    HighlightRequest,
    MediaCreateRequest,
    MediaResponse,
    MemoryDetails,
)
from beluno.modules import media
from beluno.modules.context import CommandContext
from beluno.sync.commands import (
    Command,
    CommandCall,
    EmptyPayload,
    required_version,
    version_of,
)


async def _create(
    ctx: CommandContext, call: CommandCall, body: MediaCreateRequest
) -> MediaResponse:
    created = await media.create_media(
        ctx,
        call.id("plan_id"),
        body.id,
        kind=body.kind,
        declared_type=body.content_type,
        declared_size=body.size_bytes,
        expense_id=body.expense_id,
        memory=_details(body.memory) if body.memory else None,
    )
    return media_response(created)


def _details(body: MemoryDetails) -> media.MemoryFields:
    return media.MemoryFields(
        caption=body.caption, day=body.day, taken_time=body.taken_time, place_id=body.place_id
    )


async def _update_memory(
    ctx: CommandContext, call: CommandCall, body: MemoryDetails
) -> MediaResponse:
    updated = await media.update_memory(
        ctx, call.id("plan_id"), call.id("media_id"), required_version(call), _details(body)
    )
    return media_response(updated)


async def _highlight(
    ctx: CommandContext, call: CommandCall, body: HighlightRequest
) -> MediaResponse:
    picked = await media.set_highlight(ctx, call.id("plan_id"), call.id("media_id"), body.in_recap)
    return media_response(picked)


async def _delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await media.delete_media(ctx, call.id("plan_id"), call.id("media_id"))


MEDIA_CREATE = Command(
    name="media.create",
    payload_model=MediaCreateRequest,
    response_model=MediaResponse,
    handler=_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
)
MEDIA_DELETE = Command(
    name="media.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_delete,
    target_fields=("plan_id", "media_id"),
    status=204,
)

MEDIA_UPDATE_MEMORY = Command(
    name="media.update_memory",
    payload_model=MemoryDetails,
    response_model=MediaResponse,
    handler=_update_memory,
    target_fields=("plan_id", "media_id"),
    versioned=True,
    etag=version_of,
)
MEDIA_HIGHLIGHT = Command(
    name="media.set_highlight",
    payload_model=HighlightRequest,
    response_model=MediaResponse,
    handler=_highlight,
    target_fields=("plan_id", "media_id"),
    etag=version_of,
)

COMMANDS: list[Command[Any, Any]] = [
    MEDIA_CREATE,
    MEDIA_UPDATE_MEMORY,
    MEDIA_HIGHLIGHT,
    MEDIA_DELETE,
]
