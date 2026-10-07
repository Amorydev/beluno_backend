"""Saved places: what the group might visit, who wants to go, and what made the plan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.policy import AccessState, PlanAction
from beluno.contracts.errors import conflict, not_found, version_conflict
from beluno.db.ids import new_id
from beluno.db.models.plans import PlanParticipant
from beluno.db.models.schedule_places import Place, PlaceReaction
from beluno.modules.activity.events import ActivityItem, ActivityType, item
from beluno.modules.context import CommandContext
from beluno.modules.planning import itinerary
from beluno.modules.planning.common import (
    own_participant_id,
    planning_access,
    record_planning_change,
    require_author_or_manager,
)
from beluno.modules.planning.maps_links import read_maps_link
from beluno.modules.sync_audit.recorder import ChangeScope, record_activity

PLACE_ENTITY = "place"
SHORTLIST = "shortlist"
POLL_WINNER = "poll_winner"


@dataclass(frozen=True)
class PlaceDraft:
    name: str
    maps_url: str | None
    category: str
    note: str | None


@dataclass(frozen=True)
class PlaceView:
    place: Place
    wanted_by: list[UUID]


async def list_places(ctx: CommandContext, plan_id: UUID) -> list[PlaceView]:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(Place).where(Place.plan_id == plan_id, Place.deleted_at.is_(None)).order_by(Place.id)
    )
    return await place_views(ctx, list(rows.scalars()))


async def get_place(ctx: CommandContext, plan_id: UUID, place_id: UUID) -> PlaceView:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    place = await _find(ctx, plan_id, place_id)
    return (await place_views(ctx, [place]))[0]


async def create_place(
    ctx: CommandContext,
    plan_id: UUID,
    place_id: UUID | None,
    draft: PlaceDraft,
    *,
    status: str = SHORTLIST,
) -> PlaceView:
    """``status`` is ``poll_winner`` when a poll's winning option becomes the place."""

    await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING)
    place = Place(
        id=place_id or new_id(),
        plan_id=plan_id,
        status=status,
        saved_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    _apply(place, draft)
    try:
        async with ctx.savepoint():
            ctx.session.add(place)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(
        ctx, place, "planning.place_saved", item(ActivityType.PLACE_SAVED, category=place.category)
    )
    return PlaceView(place, [])


async def update_place(
    ctx: CommandContext,
    plan_id: UUID,
    place_id: UUID,
    expected_version: int,
    draft: PlaceDraft,
) -> PlaceView:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW)
    place = await _find(ctx, plan_id, place_id, for_update=True)
    await require_author_or_manager(ctx, access, place.saved_by_user_id)
    if place.version != expected_version:
        raise version_conflict(place)
    _apply(place, draft)
    await _bump(ctx, place, "planning.place_updated")
    return (await place_views(ctx, [place]))[0]


async def delete_place(ctx: CommandContext, plan_id: UUID, place_id: UUID) -> None:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW)
    place = await _find(ctx, plan_id, place_id, for_update=True)
    await require_author_or_manager(ctx, access, place.saved_by_user_id)
    # Items already planned at this place keep pointing at it (the tombstone stays).
    place.deleted_at = ctx.now
    await _bump(ctx, place, "planning.place_deleted", operation="delete")


async def react(ctx: CommandContext, plan_id: UUID, place_id: UUID, wants: bool) -> PlaceView:
    """Set the caller's "want to go"; repeating the same answer changes nothing."""

    access = await planning_access(ctx, plan_id, PlanAction.RESPOND_PLANNING)
    place = await _find(ctx, plan_id, place_id, for_update=True)
    participant_id = own_participant_id(access)
    reaction = await ctx.session.get(PlaceReaction, (place.id, participant_id))
    if reaction is None:
        ctx.session.add(
            PlaceReaction(
                place_id=place.id,
                participant_id=participant_id,
                plan_id=plan_id,
                wants=wants,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
    elif reaction.wants == wants:
        return (await place_views(ctx, [place]))[0]
    else:
        reaction.wants = wants
        reaction.updated_at = ctx.now
    await _bump(ctx, place, "planning.place_reacted")
    return (await place_views(ctx, [place]))[0]


async def add_to_plan(
    ctx: CommandContext,
    plan_id: UUID,
    place_id: UUID,
    *,
    item_id: UUID | None,
    day: date | None,
    start_time: time | None,
    timezone: str | None,
) -> itinerary.ItemView:
    """Put the place on the itinerary as a new item (the place becomes ``in_plan``)."""

    await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING, for_update=True)
    place = await _find(ctx, plan_id, place_id, for_update=True)
    view = await itinerary.create_item(
        ctx,
        plan_id,
        item_id,
        itinerary.ItemDraft(
            title=place.name,
            day=day,
            start_time=start_time,
            timezone=timezone,
            place_id=place.id,
        ),
        announce=False,
    )
    await record_activity(
        ctx,
        item(
            ActivityType.PLACE_ADDED_TO_PLAN,
            item_id=view.item.id,
            day=day.isoformat() if day else None,
        ),
        entity_type=PLACE_ENTITY,
        entity_id=place.id,
        scope=ChangeScope.PLAN,
        scope_id=plan_id,
    )
    return view


async def place_views(ctx: CommandContext, places: list[Place]) -> list[PlaceView]:
    if not places:
        return []
    # People who left or were removed no longer count as wanting to go.
    rows = await ctx.session.execute(
        select(PlaceReaction.place_id, PlaceReaction.participant_id)
        .join(PlanParticipant, PlanParticipant.id == PlaceReaction.participant_id)
        .where(
            PlaceReaction.place_id.in_([place.id for place in places]),
            PlaceReaction.wants.is_(True),
            PlanParticipant.access_state == AccessState.ACTIVE.value,
        )
        .order_by(PlaceReaction.participant_id)
    )
    wanted: dict[UUID, list[UUID]] = {}
    for place_id, participant_id in rows.all():
        wanted.setdefault(place_id, []).append(participant_id)
    return [PlaceView(place, wanted.get(place.id, [])) for place in places]


def _apply(place: Place, draft: PlaceDraft) -> None:
    place.name = draft.name
    place.category = draft.category
    place.note = draft.note
    place.maps_url = draft.maps_url
    if draft.maps_url is None:
        place.provider, place.latitude, place.longitude = None, None, None
        place.resolution_state = "manual"
        return
    link = read_maps_link(draft.maps_url)
    place.provider, place.latitude, place.longitude = link.provider, link.latitude, link.longitude
    place.resolution_state = "parsed" if link.parsed else "pending"


async def _find(
    ctx: CommandContext, plan_id: UUID, place_id: UUID, *, for_update: bool = False
) -> Place:
    statement = select(Place).where(Place.plan_id == plan_id, Place.id == place_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    place = (await ctx.session.execute(statement)).scalar_one_or_none()
    if place is None or place.deleted_at is not None:
        raise not_found()
    return place


async def _bump(
    ctx: CommandContext,
    place: Place,
    action: str,
    *,
    operation: str = "upsert",
    activity: ActivityItem | None = None,
) -> None:
    place.version += 1
    place.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, place, action, activity, operation=operation)


async def _record(
    ctx: CommandContext,
    place: Place,
    action: str,
    activity: ActivityItem | None = None,
    *,
    operation: str = "upsert",
) -> None:
    await record_planning_change(
        ctx,
        action=action,
        entity_type=PLACE_ENTITY,
        entity_id=place.id,
        entity_version=place.version,
        plan_id=place.plan_id,
        metadata={"version": place.version, "status": place.status},
        operation=operation,
        activity=activity,
    )


async def mark_poll_winner(ctx: CommandContext, plan_id: UUID, place_id: UUID) -> PlaceView:
    """The place won a poll: it keeps that status even when items come and go."""

    place = await _find(ctx, plan_id, place_id, for_update=True)
    if place.status != POLL_WINNER:
        place.status = POLL_WINNER
        await _bump(ctx, place, "planning.place_won_poll")
    return (await place_views(ctx, [place]))[0]
