"""Trip planning as REST responses and plan-scope sync entities.

``place``, ``booking``, ``itinerary_item``, ``poll``, ``task``, and the shared
``packing_item``s; a person's private packing items sync in their user scope.
Bookings never carry their secrets, only whether each is set.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from beluno.contracts.planning import (
    AttendanceResponse,
    BookingResponse,
    ItineraryItemResponse,
    PackingItemResponse,
    PlaceResponse,
    PollOptionResponse,
    PollOutcomeResponse,
    PollResponse,
    PollResultResponse,
    TaskResponse,
)
from beluno.db.models.bookings import Booking
from beluno.db.models.coordination import PackingItem, Task
from beluno.db.models.decisions import Poll
from beluno.db.models.schedule_places import ItineraryItem, Place
from beluno.modules.context import CommandContext
from beluno.modules.planning.bookings import BookingView, booking_views
from beluno.modules.planning.itinerary import ItemView, item_views
from beluno.modules.planning.places import PlaceView, place_views
from beluno.modules.planning.polls import PollView, poll_views
from beluno.modules.sync_audit.recorder import ChangeScope
from beluno.sync.pull import SnapshotRow
from beluno.sync.scopes import AccessLevel, ScopeKey

PLANNING_TYPES = ("place", "booking", "itinerary_item", "poll", "task", "packing_item")


def place_response(view: PlaceView) -> PlaceResponse:
    place = view.place
    return PlaceResponse(
        id=place.id,
        plan_id=place.plan_id,
        name=place.name,
        maps_url=place.maps_url,
        provider=place.provider,  # type: ignore[arg-type]
        latitude=place.latitude,
        longitude=place.longitude,
        resolution_state=place.resolution_state,  # type: ignore[arg-type]
        category=place.category,  # type: ignore[arg-type]
        note=place.note,
        status=place.status,  # type: ignore[arg-type]
        saved_by_user_id=place.saved_by_user_id,
        wanted_by=view.wanted_by,
        version=place.version,
        created_at=place.created_at,
        updated_at=place.updated_at,
    )


def item_response(view: ItemView) -> ItineraryItemResponse:
    entry = view.item
    return ItineraryItemResponse(
        id=entry.id,
        plan_id=entry.plan_id,
        title=entry.title,
        day=entry.day,
        start_time=entry.start_time,
        timezone=entry.timezone,
        duration_minutes=entry.duration_minutes,
        note=entry.note,
        place_id=entry.place_id,
        lead_participant_id=entry.lead_participant_id,
        booking_id=entry.booking_id,
        status=entry.status,  # type: ignore[arg-type]
        order_key=entry.order_key,
        attendance=[
            AttendanceResponse(participant_id=answer.participant_id, status=answer.status)  # type: ignore[arg-type]
            for answer in view.attendance
        ],
        commitment_id=view.commitment_id,
        created_by_user_id=entry.created_by_user_id,
        version=entry.version,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def booking_response(view: BookingView) -> BookingResponse:
    booking = view.booking
    return BookingResponse(
        id=booking.id,
        plan_id=booking.plan_id,
        kind=booking.kind,  # type: ignore[arg-type]
        title=booking.title,
        provider=booking.provider,
        start_date=booking.start_date,
        start_time=booking.start_time,
        start_timezone=booking.start_timezone,
        end_date=booking.end_date,
        end_time=booking.end_time,
        end_timezone=booking.end_timezone,
        place_id=booking.place_id,
        traveler_ids=list(booking.traveler_ids),
        status=booking.status,  # type: ignore[arg-type]
        payment_note=booking.payment_note,  # type: ignore[arg-type]
        free_cancellation_until=booking.free_cancellation_until,
        has_confirmation_code=booking.has_confirmation_code,
        has_private_notes=booking.has_private_notes,
        commitment_id=view.commitment_id,
        created_by_user_id=booking.created_by_user_id,
        version=booking.version,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
    )


def task_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        plan_id=task.plan_id,
        title=task.title,
        note=task.note,
        assignee_participant_id=task.assignee_participant_id,
        due_date=task.due_date,
        due_time=task.due_time,
        due_timezone=task.due_timezone,
        remind_at=task.remind_at,
        status=task.status,  # type: ignore[arg-type]
        completed_at=task.completed_at,
        completed_by_user_id=task.completed_by_user_id,
        item_id=task.item_id,
        booking_id=task.booking_id,
        created_by_user_id=task.created_by_user_id,
        version=task.version,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def packing_response(entry: PackingItem) -> PackingItemResponse:
    return PackingItemResponse(
        id=entry.id,
        plan_id=entry.plan_id,
        visibility=entry.visibility,  # type: ignore[arg-type]
        owner_user_id=entry.owner_user_id,
        name=entry.name,
        category=entry.category,  # type: ignore[arg-type]
        quantity=entry.quantity,
        bringer_participant_id=entry.bringer_participant_id,
        packed=entry.packed,
        template_id=entry.template_id,
        created_by_user_id=entry.created_by_user_id,
        version=entry.version,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def poll_response(view: PollView) -> PollResponse:
    poll, result = view.poll, view.result
    voters: dict[UUID, list[UUID]] = {}
    for ballot in view.votes:
        voters.setdefault(ballot.option_id, []).append(ballot.participant_id)
    return PollResponse(
        id=poll.id,
        plan_id=poll.plan_id,
        kind=poll.kind,  # type: ignore[arg-type]
        question=poll.question,
        options=[
            PollOptionResponse(
                id=option.id,
                label=option.label,
                place_id=option.place_id,
                answer=option.answer,  # type: ignore[arg-type]
                position=option.position,
                voter_ids=voters.get(option.id, []),
            )
            for option in view.options
        ],
        deadline_at=poll.deadline_at,
        quorum=poll.quorum,
        allow_vote_change=poll.allow_vote_change,
        status=poll.status,  # type: ignore[arg-type]
        eligible=view.eligible,
        result=PollResultResponse(
            version=result.version,
            outcome=result.outcome,  # type: ignore[arg-type]
            winner_option_id=result.winner_option_id,
            tied_option_ids=list(result.tied_option_ids),
            counts={key: int(value) for key, value in result.counts.items()},
            eligible=result.eligible,
            voted=result.voted,
            closed_at=result.closed_at,
            closed_by_user_id=result.closed_by_user_id,
        )
        if result is not None
        else None,
        outcomes=[
            PollOutcomeResponse(
                action=outcome.action,  # type: ignore[arg-type]
                option_id=outcome.option_id,
                created_entity_id=outcome.created_entity_id,
            )
            for outcome in view.outcomes
        ],
        created_by_user_id=poll.created_by_user_id,
        version=poll.version,
        created_at=poll.created_at,
        updated_at=poll.updated_at,
    )


async def present_planning_current(ctx: CommandContext, entity: object) -> BaseModel | None:
    """The current row behind a version conflict, as the caller would read it."""

    if isinstance(entity, Place):
        return place_response((await place_views(ctx, [entity]))[0])
    if isinstance(entity, ItineraryItem):
        return item_response((await item_views(ctx, [entity]))[0])
    if isinstance(entity, Poll):
        return poll_response((await poll_views(ctx, [entity]))[0])
    if isinstance(entity, Booking):
        return booking_response((await booking_views(ctx, [entity]))[0])
    if isinstance(entity, Task):
        return task_response(entity)
    if isinstance(entity, PackingItem):
        return packing_response(entity)
    return None


async def load_place(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    place = await ctx.session.get(Place, id)
    if place is None or place.plan_id != scope.scope_id or place.deleted_at is not None:
        return None
    return place_response((await place_views(ctx, [place]))[0])


async def page_places(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Place).where(Place.plan_id == scope.scope_id, Place.deleted_at.is_(None))
    if after is not None:
        statement = statement.where(Place.id > after)
    rows = list((await ctx.session.execute(statement.order_by(Place.id).limit(limit))).scalars())
    return [
        SnapshotRow(view.place.id, view.place.version, place_response(view))
        for view in await place_views(ctx, rows)
    ]


async def load_item(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    entry = await ctx.session.get(ItineraryItem, id)
    if entry is None or entry.plan_id != scope.scope_id or entry.deleted_at is not None:
        return None
    return item_response((await item_views(ctx, [entry]))[0])


async def page_items(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(ItineraryItem).where(
        ItineraryItem.plan_id == scope.scope_id, ItineraryItem.deleted_at.is_(None)
    )
    if after is not None:
        statement = statement.where(ItineraryItem.id > after)
    rows = list(
        (await ctx.session.execute(statement.order_by(ItineraryItem.id).limit(limit))).scalars()
    )
    return [
        SnapshotRow(view.item.id, view.item.version, item_response(view))
        for view in await item_views(ctx, rows)
    ]


async def load_poll(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    poll = await ctx.session.get(Poll, id)
    if poll is None or poll.plan_id != scope.scope_id or poll.deleted_at is not None:
        return None
    return poll_response((await poll_views(ctx, [poll]))[0])


async def page_polls(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Poll).where(Poll.plan_id == scope.scope_id, Poll.deleted_at.is_(None))
    if after is not None:
        statement = statement.where(Poll.id > after)
    rows = list((await ctx.session.execute(statement.order_by(Poll.id).limit(limit))).scalars())
    return [
        SnapshotRow(view.poll.id, view.poll.version, poll_response(view))
        for view in await poll_views(ctx, rows)
    ]


async def load_booking(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    booking = await ctx.session.get(Booking, id)
    if booking is None or booking.plan_id != scope.scope_id or booking.deleted_at is not None:
        return None
    return booking_response((await booking_views(ctx, [booking]))[0])


async def page_bookings(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Booking).where(
        Booking.plan_id == scope.scope_id, Booking.deleted_at.is_(None)
    )
    if after is not None:
        statement = statement.where(Booking.id > after)
    rows = list((await ctx.session.execute(statement.order_by(Booking.id).limit(limit))).scalars())
    return [
        SnapshotRow(view.booking.id, view.booking.version, booking_response(view))
        for view in await booking_views(ctx, rows)
    ]


async def load_task(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    task = await ctx.session.get(Task, id)
    if task is None or task.plan_id != scope.scope_id or task.deleted_at is not None:
        return None
    return task_response(task)


async def page_tasks(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Task).where(Task.plan_id == scope.scope_id, Task.deleted_at.is_(None))
    if after is not None:
        statement = statement.where(Task.id > after)
    rows = (await ctx.session.execute(statement.order_by(Task.id).limit(limit))).scalars()
    return [SnapshotRow(row.id, row.version, task_response(row)) for row in rows]


def _in_scope(entry: PackingItem, scope: ScopeKey) -> bool:
    """Shared items belong to their plan's scope, private ones to their owner's."""

    if scope.scope_type is ChangeScope.USER:
        return entry.visibility == "private" and entry.owner_user_id == scope.scope_id
    return entry.visibility == "shared" and entry.plan_id == scope.scope_id


async def load_packing(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    entry = await ctx.session.get(PackingItem, id)
    if entry is None or entry.deleted_at is not None or not _in_scope(entry, scope):
        return None
    return packing_response(entry)


async def page_packing(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(PackingItem).where(PackingItem.deleted_at.is_(None))
    if scope.scope_type is ChangeScope.USER:
        statement = statement.where(
            PackingItem.visibility == "private", PackingItem.owner_user_id == scope.scope_id
        )
    else:
        statement = statement.where(
            PackingItem.visibility == "shared", PackingItem.plan_id == scope.scope_id
        )
    if after is not None:
        statement = statement.where(PackingItem.id > after)
    rows = (await ctx.session.execute(statement.order_by(PackingItem.id).limit(limit))).scalars()
    return [SnapshotRow(row.id, row.version, packing_response(row)) for row in rows]
