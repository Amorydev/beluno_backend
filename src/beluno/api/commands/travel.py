"""Travel extension commands: details and ordered segments."""

from __future__ import annotations

from typing import Any

from beluno.api.commands.groups import required_version
from beluno.api.presenters import segment_input, segment_response, travel_response
from beluno.contracts.plans import (
    TravelDetailsRequest,
    TravelResponse,
    TravelSegmentRequest,
    TravelSegmentResponse,
)
from beluno.modules.context import CommandContext
from beluno.modules.plans import travel
from beluno.sync.commands import Command, CommandCall, EmptyPayload, version_of


async def _put_details(
    ctx: CommandContext, call: CommandCall, body: TravelDetailsRequest
) -> TravelResponse:
    # Creation needs no version; replacing the details requires the current one.
    view = await travel.put_details(
        ctx,
        call.id("plan_id"),
        destination_summary=body.destination_summary,
        notes=body.notes,
        expected_version=call.expected_version,
    )
    return travel_response(view)


async def _add_segment(
    ctx: CommandContext, call: CommandCall, body: TravelSegmentRequest
) -> TravelSegmentResponse:
    segment = await travel.add_segment(ctx, call.id("plan_id"), segment_input(body))
    return segment_response(segment)


async def _update_segment(
    ctx: CommandContext, call: CommandCall, body: TravelSegmentRequest
) -> TravelSegmentResponse:
    segment = await travel.update_segment(
        ctx, call.id("plan_id"), call.id("segment_id"), segment_input(body), required_version(call)
    )
    return segment_response(segment)


async def _delete_segment(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await travel.delete_segment(ctx, call.id("plan_id"), call.id("segment_id"))


TRAVEL_PUT_DETAILS = Command(
    name="travel.put_details",
    payload_model=TravelDetailsRequest,
    response_model=TravelResponse,
    handler=_put_details,
    target_fields=("plan_id",),
    etag=version_of,
)
TRAVEL_SEGMENT_ADD = Command(
    name="travel.segment.add",
    payload_model=TravelSegmentRequest,
    response_model=TravelSegmentResponse,
    handler=_add_segment,
    target_fields=("plan_id",),
    status=201,
)
TRAVEL_SEGMENT_UPDATE = Command(
    name="travel.segment.update",
    payload_model=TravelSegmentRequest,
    response_model=TravelSegmentResponse,
    handler=_update_segment,
    target_fields=("plan_id", "segment_id"),
    versioned=True,
)
TRAVEL_SEGMENT_DELETE = Command(
    name="travel.segment.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_delete_segment,
    target_fields=("plan_id", "segment_id"),
    status=204,
)

COMMANDS: list[Command[Any, Any]] = [
    TRAVEL_PUT_DETAILS,
    TRAVEL_SEGMENT_ADD,
    TRAVEL_SEGMENT_UPDATE,
    TRAVEL_SEGMENT_DELETE,
]
