"""Trip planning commands: places and the itinerary."""

from __future__ import annotations

from typing import Any

from beluno.api.planning_projection import item_response, place_response
from beluno.contracts.planning import (
    AddPlaceToPlanRequest,
    AttendanceRequest,
    ItineraryItemCreateRequest,
    ItineraryItemRequest,
    ItineraryItemResponse,
    PlaceCreateRequest,
    PlaceReactionRequest,
    PlaceRequest,
    PlaceResponse,
)
from beluno.modules.context import CommandContext
from beluno.modules.iam.rate_limits import FINANCE_WRITES_PER_PLAN
from beluno.modules.planning import itinerary, places
from beluno.sync.commands import Command, CommandCall, EmptyPayload, required_version, version_of


def _place_draft(body: PlaceRequest) -> places.PlaceDraft:
    return places.PlaceDraft(
        name=body.name, maps_url=body.maps_url, category=body.category, note=body.note
    )


def _item_draft(body: ItineraryItemRequest) -> itinerary.ItemDraft:
    cost = body.estimated_cost
    return itinerary.ItemDraft(
        title=body.title,
        day=body.day,
        start_time=body.start_time,
        timezone=body.timezone,
        duration_minutes=body.duration_minutes,
        note=body.note,
        place_id=body.place_id,
        lead_participant_id=body.lead_participant_id,
        status=body.status,
        order_key=body.order_key,
        estimated_cost=itinerary.EstimatedCost(cost.currency, cost.amount_minor, cost.category)
        if cost
        else None,
    )


async def _place_create(
    ctx: CommandContext, call: CommandCall, body: PlaceCreateRequest
) -> PlaceResponse:
    view = await places.create_place(ctx, call.id("plan_id"), body.id, _place_draft(body))
    return place_response(view)


async def _place_update(
    ctx: CommandContext, call: CommandCall, body: PlaceRequest
) -> PlaceResponse:
    view = await places.update_place(
        ctx, call.id("plan_id"), call.id("place_id"), required_version(call), _place_draft(body)
    )
    return place_response(view)


async def _place_delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await places.delete_place(ctx, call.id("plan_id"), call.id("place_id"))


async def _place_react(
    ctx: CommandContext, call: CommandCall, body: PlaceReactionRequest
) -> PlaceResponse:
    view = await places.react(ctx, call.id("plan_id"), call.id("place_id"), body.wants)
    return place_response(view)


async def _place_add_to_plan(
    ctx: CommandContext, call: CommandCall, body: AddPlaceToPlanRequest
) -> ItineraryItemResponse:
    view = await places.add_to_plan(
        ctx,
        call.id("plan_id"),
        call.id("place_id"),
        item_id=body.item_id,
        day=body.day,
        start_time=body.start_time,
        timezone=body.timezone,
    )
    return item_response(view)


async def _item_create(
    ctx: CommandContext, call: CommandCall, body: ItineraryItemCreateRequest
) -> ItineraryItemResponse:
    view = await itinerary.create_item(ctx, call.id("plan_id"), body.id, _item_draft(body))
    return item_response(view)


async def _item_update(
    ctx: CommandContext, call: CommandCall, body: ItineraryItemRequest
) -> ItineraryItemResponse:
    view = await itinerary.update_item(
        ctx, call.id("plan_id"), call.id("item_id"), required_version(call), _item_draft(body)
    )
    return item_response(view)


async def _item_delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await itinerary.delete_item(ctx, call.id("plan_id"), call.id("item_id"))


async def _item_attend(
    ctx: CommandContext, call: CommandCall, body: AttendanceRequest
) -> ItineraryItemResponse:
    view = await itinerary.attend(ctx, call.id("plan_id"), call.id("item_id"), body.status)
    return item_response(view)


PLACE = ("plan_id", "place_id")
ITEM = ("plan_id", "item_id")

PLACE_CREATE = Command(
    name="place.create",
    payload_model=PlaceCreateRequest,
    response_model=PlaceResponse,
    handler=_place_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
)
PLACE_UPDATE = Command(
    name="place.update",
    payload_model=PlaceRequest,
    response_model=PlaceResponse,
    handler=_place_update,
    target_fields=PLACE,
    versioned=True,
    etag=version_of,
)
PLACE_DELETE = Command(
    name="place.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_place_delete,
    target_fields=PLACE,
    status=204,
)
PLACE_REACT = Command(
    name="place.react",
    payload_model=PlaceReactionRequest,
    response_model=PlaceResponse,
    handler=_place_react,
    target_fields=PLACE,
    etag=version_of,
)
PLACE_ADD_TO_PLAN = Command(
    name="place.add_to_plan",
    payload_model=AddPlaceToPlanRequest,
    response_model=ItineraryItemResponse,
    handler=_place_add_to_plan,
    target_fields=PLACE,
    status=201,
    etag=version_of,
    # It may record a cost commitment: the finance write limit applies.
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
ITEM_CREATE = Command(
    name="itinerary.create",
    payload_model=ItineraryItemCreateRequest,
    response_model=ItineraryItemResponse,
    handler=_item_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    # It may record a cost commitment: the finance write limit applies.
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
ITEM_UPDATE = Command(
    name="itinerary.update",
    payload_model=ItineraryItemRequest,
    response_model=ItineraryItemResponse,
    handler=_item_update,
    target_fields=ITEM,
    versioned=True,
    etag=version_of,
    # It may record a cost commitment: the finance write limit applies.
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
ITEM_DELETE = Command(
    name="itinerary.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_item_delete,
    target_fields=ITEM,
    status=204,
)
ITEM_ATTEND = Command(
    name="itinerary.attend",
    payload_model=AttendanceRequest,
    response_model=ItineraryItemResponse,
    handler=_item_attend,
    target_fields=ITEM,
    etag=version_of,
)

COMMANDS: list[Command[Any, Any]] = [
    PLACE_CREATE,
    PLACE_UPDATE,
    PLACE_DELETE,
    PLACE_REACT,
    PLACE_ADD_TO_PLAN,
    ITEM_CREATE,
    ITEM_UPDATE,
    ITEM_DELETE,
    ITEM_ATTEND,
]
