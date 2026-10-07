"""Crew commands: create, rename or change people, delete."""

from __future__ import annotations

from typing import Any

from beluno.api.presenters import crew_response
from beluno.contracts.people import CrewCreateRequest, CrewResponse, CrewUpdateRequest
from beluno.modules.context import CommandContext
from beluno.modules.people import crews
from beluno.sync.commands import Command, CommandCall, EmptyPayload, required_version, version_of


async def _create(ctx: CommandContext, call: CommandCall, body: CrewCreateRequest) -> CrewResponse:
    view = await crews.create_crew(
        ctx,
        body.id,
        body.name,
        member_user_ids=body.member_user_ids,
        from_plan_id=body.from_plan_id,
    )
    return crew_response(view)


async def _update(ctx: CommandContext, call: CommandCall, body: CrewUpdateRequest) -> CrewResponse:
    view = await crews.update_crew(
        ctx,
        call.id("crew_id"),
        required_version(call),
        name=body.name,
        member_user_ids=body.member_user_ids,
    )
    return crew_response(view)


async def _delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await crews.delete_crew(ctx, call.id("crew_id"))


CREW_CREATE = Command(
    name="crew.create",
    payload_model=CrewCreateRequest,
    response_model=CrewResponse,
    handler=_create,
    status=201,
    etag=version_of,
)
CREW_UPDATE = Command(
    name="crew.update",
    payload_model=CrewUpdateRequest,
    response_model=CrewResponse,
    handler=_update,
    target_fields=("crew_id",),
    versioned=True,
    etag=version_of,
)
CREW_DELETE = Command(
    name="crew.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_delete,
    target_fields=("crew_id",),
    status=204,
)

COMMANDS: list[Command[Any, Any]] = [CREW_CREATE, CREW_UPDATE, CREW_DELETE]
