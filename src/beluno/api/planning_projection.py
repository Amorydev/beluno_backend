"""Trip planning as REST responses and plan-scope sync entities (``place``, ``itinerary_item``)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from beluno.contracts.planning import AttendanceResponse, ItineraryItemResponse, PlaceResponse
from beluno.db.models.schedule_places import ItineraryItem, Place
from beluno.modules.context import CommandContext
from beluno.modules.planning.itinerary import ItemView, item_views
from beluno.modules.planning.places import PlaceView, place_views
from beluno.sync.pull import SnapshotRow
from beluno.sync.scopes import AccessLevel, ScopeKey

PLANNING_TYPES = ("place", "itinerary_item")


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


async def present_planning_current(ctx: CommandContext, entity: object) -> BaseModel | None:
    """The current row behind a version conflict, as the caller would read it."""

    if isinstance(entity, Place):
        return place_response((await place_views(ctx, [entity]))[0])
    if isinstance(entity, ItineraryItem):
        return item_response((await item_views(ctx, [entity]))[0])
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
