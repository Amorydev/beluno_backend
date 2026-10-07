"""Trip planning commands: places, the itinerary, polls, bookings, tasks, and packing."""

from __future__ import annotations

from typing import Any

from beluno.api.planning_projection import (
    booking_response,
    item_response,
    packing_response,
    place_response,
    poll_response,
    task_response,
)
from beluno.contracts.planning import (
    AddPlaceToPlanRequest,
    AttendanceRequest,
    BookingCreateRequest,
    BookingRequest,
    BookingResponse,
    ItineraryItemCreateRequest,
    ItineraryItemRequest,
    ItineraryItemResponse,
    PackedRequest,
    PackingItemCreateRequest,
    PackingItemRequest,
    PackingItemResponse,
    PackingListResponse,
    PackingTemplateRequest,
    PlaceCreateRequest,
    PlaceReactionRequest,
    PlaceRequest,
    PlaceResponse,
    PollCreateRequest,
    PollOutcomeRequest,
    PollResponse,
    TaskCreateRequest,
    TaskRequest,
    TaskResponse,
    TaskStatusRequest,
    VoteRequest,
)
from beluno.modules.context import CommandContext
from beluno.modules.iam.rate_limits import FINANCE_WRITES_PER_PLAN
from beluno.modules.planning import bookings, itinerary, packing, places, polls, tasks
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
        booking_id=body.booking_id,
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


async def _poll_create(
    ctx: CommandContext, call: CommandCall, body: PollCreateRequest
) -> PollResponse:
    draft = polls.PollDraft(
        kind=body.kind,
        question=body.question,
        options=[polls.OptionDraft(option.label, option.place_id) for option in body.options],
        deadline_at=body.deadline_at,
        quorum=body.quorum,
        allow_vote_change=body.allow_vote_change,
    )
    return poll_response(await polls.create_poll(ctx, call.id("plan_id"), body.id, draft))


async def _poll_vote(ctx: CommandContext, call: CommandCall, body: VoteRequest) -> PollResponse:
    view = await polls.vote(ctx, call.id("plan_id"), call.id("poll_id"), body.option_id)
    return poll_response(view)


async def _poll_close(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> PollResponse:
    return poll_response(await polls.close_poll(ctx, call.id("plan_id"), call.id("poll_id")))


async def _poll_delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await polls.delete_poll(ctx, call.id("plan_id"), call.id("poll_id"))


async def _poll_apply(
    ctx: CommandContext, call: CommandCall, body: PollOutcomeRequest
) -> PollResponse:
    request = polls.OutcomeRequest(
        action=body.action,
        result_version=body.result_version,
        option_id=body.option_id,
        day=body.day,
        start_time=body.start_time,
        timezone=body.timezone,
    )
    view = await polls.apply_outcome(ctx, call.id("plan_id"), call.id("poll_id"), request)
    return poll_response(view)


def _booking_draft(body: BookingRequest) -> bookings.BookingDraft:
    price, secrets = body.price, body.secrets
    return bookings.BookingDraft(
        kind=body.kind,
        title=body.title,
        provider=body.provider,
        start_date=body.start_date,
        start_time=body.start_time,
        start_timezone=body.start_timezone,
        end_date=body.end_date,
        end_time=body.end_time,
        end_timezone=body.end_timezone,
        place_id=body.place_id,
        traveler_ids=tuple(body.traveler_ids),
        status=body.status,
        payment_note=body.payment_note,
        free_cancellation_until=body.free_cancellation_until,
        price=itinerary.EstimatedCost(price.currency, price.amount_minor, price.category)
        if price
        else None,
        secrets=bookings.Secrets(
            confirmation_code=secrets.confirmation_code
            if "confirmation_code" in secrets.model_fields_set
            else bookings.KEEP,
            private_notes=secrets.private_notes
            if "private_notes" in secrets.model_fields_set
            else bookings.KEEP,
        )
        if secrets
        else None,
    )


async def _booking_create(
    ctx: CommandContext, call: CommandCall, body: BookingCreateRequest
) -> BookingResponse:
    view = await bookings.create_booking(ctx, call.id("plan_id"), body.id, _booking_draft(body))
    return booking_response(view)


async def _booking_update(
    ctx: CommandContext, call: CommandCall, body: BookingRequest
) -> BookingResponse:
    view = await bookings.update_booking(
        ctx, call.id("plan_id"), call.id("booking_id"), required_version(call), _booking_draft(body)
    )
    return booking_response(view)


async def _booking_delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await bookings.delete_booking(ctx, call.id("plan_id"), call.id("booking_id"))


def _task_draft(body: TaskRequest) -> tasks.TaskDraft:
    return tasks.TaskDraft(
        title=body.title,
        note=body.note,
        assignee_participant_id=body.assignee_participant_id,
        due_date=body.due_date,
        due_time=body.due_time,
        due_timezone=body.due_timezone,
        remind_at=body.remind_at,
        status=body.status,
        item_id=body.item_id,
        booking_id=body.booking_id,
    )


async def _task_create(
    ctx: CommandContext, call: CommandCall, body: TaskCreateRequest
) -> TaskResponse:
    task = await tasks.create_task(ctx, call.id("plan_id"), body.id, _task_draft(body))
    return task_response(task)


async def _task_update(ctx: CommandContext, call: CommandCall, body: TaskRequest) -> TaskResponse:
    task = await tasks.update_task(
        ctx, call.id("plan_id"), call.id("task_id"), required_version(call), _task_draft(body)
    )
    return task_response(task)


async def _task_set_status(
    ctx: CommandContext, call: CommandCall, body: TaskStatusRequest
) -> TaskResponse:
    task = await tasks.set_status(ctx, call.id("plan_id"), call.id("task_id"), body.status)
    return task_response(task)


async def _task_delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await tasks.delete_task(ctx, call.id("plan_id"), call.id("task_id"))


def _packing_draft(body: PackingItemRequest) -> packing.PackingDraft:
    return packing.PackingDraft(
        name=body.name,
        category=body.category,
        quantity=body.quantity,
        bringer_participant_id=body.bringer_participant_id,
    )


async def _packing_create(
    ctx: CommandContext, call: CommandCall, body: PackingItemCreateRequest
) -> PackingItemResponse:
    entry = await packing.create_item(
        ctx, call.id("plan_id"), body.id, body.visibility, _packing_draft(body)
    )
    return packing_response(entry)


async def _packing_update(
    ctx: CommandContext, call: CommandCall, body: PackingItemRequest
) -> PackingItemResponse:
    entry = await packing.update_item(
        ctx,
        call.id("plan_id"),
        call.id("packing_item_id"),
        required_version(call),
        _packing_draft(body),
    )
    return packing_response(entry)


async def _packing_set_packed(
    ctx: CommandContext, call: CommandCall, body: PackedRequest
) -> PackingItemResponse:
    entry = await packing.set_packed(
        ctx, call.id("plan_id"), call.id("packing_item_id"), body.packed
    )
    return packing_response(entry)


async def _packing_share(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> PackingItemResponse:
    entry = await packing.share_item(ctx, call.id("plan_id"), call.id("packing_item_id"))
    return packing_response(entry)


async def _packing_delete(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await packing.delete_item(ctx, call.id("plan_id"), call.id("packing_item_id"))


async def _packing_apply_template(
    ctx: CommandContext, call: CommandCall, body: PackingTemplateRequest
) -> PackingListResponse:
    entries = await packing.apply_template(
        ctx,
        call.id("plan_id"),
        body.template_id,
        body.visibility,
        [_packing_draft(entry) for entry in body.items],
    )
    return PackingListResponse(items=[packing_response(entry) for entry in entries])


PLACE = ("plan_id", "place_id")
BOOKING = ("plan_id", "booking_id")
POLL = ("plan_id", "poll_id")
ITEM = ("plan_id", "item_id")
TASK = ("plan_id", "task_id")
PACKING = ("plan_id", "packing_item_id")

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

POLL_CREATE = Command(
    name="poll.create",
    payload_model=PollCreateRequest,
    response_model=PollResponse,
    handler=_poll_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
)
POLL_VOTE = Command(
    name="poll.vote",
    payload_model=VoteRequest,
    response_model=PollResponse,
    handler=_poll_vote,
    target_fields=POLL,
    etag=version_of,
)
POLL_CLOSE = Command(
    name="poll.close",
    payload_model=EmptyPayload,
    response_model=PollResponse,
    handler=_poll_close,
    target_fields=POLL,
    etag=version_of,
)
POLL_DELETE = Command(
    name="poll.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_poll_delete,
    target_fields=POLL,
    status=204,
)
POLL_APPLY_OUTCOME = Command(
    name="poll.apply_outcome",
    payload_model=PollOutcomeRequest,
    response_model=PollResponse,
    handler=_poll_apply,
    target_fields=POLL,
    etag=version_of,
    # Putting the winner on the itinerary may record a cost later: same write limit.
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)

BOOKING_CREATE = Command(
    name="booking.create",
    payload_model=BookingCreateRequest,
    response_model=BookingResponse,
    handler=_booking_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
BOOKING_UPDATE = Command(
    name="booking.update",
    payload_model=BookingRequest,
    response_model=BookingResponse,
    handler=_booking_update,
    target_fields=BOOKING,
    versioned=True,
    etag=version_of,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
BOOKING_DELETE = Command(
    name="booking.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_booking_delete,
    target_fields=BOOKING,
    status=204,
)

TASK_CREATE = Command(
    name="task.create",
    payload_model=TaskCreateRequest,
    response_model=TaskResponse,
    handler=_task_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
)
TASK_UPDATE = Command(
    name="task.update",
    payload_model=TaskRequest,
    response_model=TaskResponse,
    handler=_task_update,
    target_fields=TASK,
    versioned=True,
    etag=version_of,
)
TASK_SET_STATUS = Command(
    name="task.set_status",
    payload_model=TaskStatusRequest,
    response_model=TaskResponse,
    handler=_task_set_status,
    target_fields=TASK,
    etag=version_of,
)
TASK_DELETE = Command(
    name="task.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_task_delete,
    target_fields=TASK,
    status=204,
)

PACKING_CREATE = Command(
    name="packing.create",
    payload_model=PackingItemCreateRequest,
    response_model=PackingItemResponse,
    handler=_packing_create,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
)
PACKING_UPDATE = Command(
    name="packing.update",
    payload_model=PackingItemRequest,
    response_model=PackingItemResponse,
    handler=_packing_update,
    target_fields=PACKING,
    versioned=True,
    etag=version_of,
)
PACKING_SET_PACKED = Command(
    name="packing.set_packed",
    payload_model=PackedRequest,
    response_model=PackingItemResponse,
    handler=_packing_set_packed,
    target_fields=PACKING,
    etag=version_of,
)
PACKING_SHARE = Command(
    name="packing.share",
    payload_model=EmptyPayload,
    response_model=PackingItemResponse,
    handler=_packing_share,
    target_fields=PACKING,
    etag=version_of,
)
PACKING_DELETE = Command(
    name="packing.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_packing_delete,
    target_fields=PACKING,
    status=204,
)
PACKING_APPLY_TEMPLATE = Command(
    name="packing.apply_template",
    payload_model=PackingTemplateRequest,
    response_model=PackingListResponse,
    handler=_packing_apply_template,
    target_fields=("plan_id",),
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
    POLL_CREATE,
    POLL_VOTE,
    POLL_CLOSE,
    POLL_DELETE,
    POLL_APPLY_OUTCOME,
    BOOKING_CREATE,
    BOOKING_UPDATE,
    BOOKING_DELETE,
    TASK_CREATE,
    TASK_UPDATE,
    TASK_SET_STATUS,
    TASK_DELETE,
    PACKING_CREATE,
    PACKING_UPDATE,
    PACKING_SET_PACKED,
    PACKING_SHARE,
    PACKING_DELETE,
    PACKING_APPLY_TEMPLATE,
]
