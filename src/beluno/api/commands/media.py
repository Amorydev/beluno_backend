"""Media commands that work offline: record a file, or delete one."""

from __future__ import annotations

from typing import Any

from beluno.api.media_projection import media_response
from beluno.contracts.media import MediaCreateRequest, MediaResponse
from beluno.modules import media
from beluno.modules.context import CommandContext
from beluno.sync.commands import Command, CommandCall, EmptyPayload, version_of


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
    )
    return media_response(created)


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

COMMANDS: list[Command[Any, Any]] = [MEDIA_CREATE, MEDIA_DELETE]
