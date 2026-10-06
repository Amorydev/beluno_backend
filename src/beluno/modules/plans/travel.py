"""Optional travel extension: one details row per plan plus ordered segments.

Nothing in the core plan depends on it. Segment times are entered as local wall
clock + IANA zone (a red-eye departs in one zone and lands in another); the UTC
instant is derived once and both are stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import conflict, not_found, validation_error, version_conflict
from beluno.db.ids import new_id
from beluno.db.models.plans import TravelPlanDetails, TravelSegment
from beluno.modules.context import CommandContext
from beluno.modules.plans.timing import GapPolicy, check_date_range, resolve_local
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation


@dataclass(frozen=True)
class SegmentInput:
    segment_id: UUID | None
    segment_type: str
    title: str | None
    origin_label: str | None
    destination_label: str | None
    timing_mode: str
    start_date: date | None
    end_date: date | None
    departure_local: datetime | None
    departure_timezone: str | None
    arrival_local: datetime | None
    arrival_timezone: str | None
    sort_order: int


@dataclass(frozen=True)
class TravelView:
    details: TravelPlanDetails
    segments: list[TravelSegment]


async def get_travel(ctx: CommandContext, plan_id: UUID) -> TravelView:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_TRAVEL)
    details = await ctx.session.get(TravelPlanDetails, plan_id)
    if details is None:
        raise not_found()
    return TravelView(details=details, segments=await _segments(ctx, plan_id))


async def put_details(
    ctx: CommandContext,
    plan_id: UUID,
    *,
    destination_summary: str | None,
    notes: str | None,
    expected_version: int | None,
) -> TravelView:
    """Create the extension (no If-Match) or replace its fields (If-Match required)."""

    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_TRAVEL)
    details = await ctx.session.get(TravelPlanDetails, plan_id, with_for_update=True)
    if details is None:
        details = TravelPlanDetails(
            plan_id=plan_id,
            destination_summary=destination_summary,
            notes=notes,
            version=1,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        ctx.session.add(details)
        action = "travel.details_created"
    else:
        if expected_version is None or details.version != expected_version:
            raise version_conflict(details)
        details.destination_summary = destination_summary
        details.notes = notes
        details.version += 1
        details.updated_at = ctx.now
        action = "travel.details_updated"
    await ctx.session.flush()
    await _record(ctx, plan_id, "travel_details", plan_id, details.version, action)
    return TravelView(details=details, segments=await _segments(ctx, plan_id))


async def add_segment(ctx: CommandContext, plan_id: UUID, data: SegmentInput) -> TravelSegment:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_TRAVEL)
    if await ctx.session.get(TravelPlanDetails, plan_id) is None:
        raise validation_error("Add travel details before adding segments")
    segment = TravelSegment(
        id=data.segment_id or new_id(),
        plan_id=plan_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    _apply_segment(segment, data)
    try:
        async with ctx.savepoint():
            ctx.session.add(segment)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(ctx, plan_id, "travel_segment", segment.id, 1, "travel.segment_added")
    return segment


async def update_segment(
    ctx: CommandContext,
    plan_id: UUID,
    segment_id: UUID,
    data: SegmentInput,
    expected_version: int,
) -> TravelSegment:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_TRAVEL)
    segment = await _segment(ctx, plan_id, segment_id)
    if segment.version != expected_version:
        raise version_conflict(segment)
    _apply_segment(segment, data)
    segment.version += 1
    segment.updated_at = ctx.now
    await ctx.session.flush()
    await _record(
        ctx, plan_id, "travel_segment", segment.id, segment.version, "travel.segment_updated"
    )
    return segment


async def delete_segment(ctx: CommandContext, plan_id: UUID, segment_id: UUID) -> None:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_TRAVEL)
    segment = await _segment(ctx, plan_id, segment_id)
    segment.deleted_at = ctx.now
    segment.version += 1
    segment.updated_at = ctx.now
    await ctx.session.flush()
    await _record(
        ctx,
        plan_id,
        "travel_segment",
        segment.id,
        segment.version,
        "travel.segment_deleted",
        operation="delete",
    )


def _apply_segment(segment: TravelSegment, data: SegmentInput) -> None:
    segment.segment_type = data.segment_type
    segment.title = data.title
    segment.origin_label = data.origin_label
    segment.destination_label = data.destination_label
    segment.timing_mode = data.timing_mode
    segment.sort_order = data.sort_order
    if data.timing_mode == "date":
        assert data.start_date is not None
        check_date_range(data.start_date, data.end_date)
        segment.start_date, segment.end_date = data.start_date, data.end_date
        segment.departure_local = segment.departure_timezone = segment.departs_at = None
        segment.arrival_local = segment.arrival_timezone = segment.arrives_at = None
        return
    assert data.departure_local is not None and data.departure_timezone is not None
    departs_at = resolve_local(
        data.departure_local, data.departure_timezone, on_gap=GapPolicy.REJECT
    )
    arrives_at = None
    if data.arrival_local is not None and data.arrival_timezone is not None:
        arrives_at = resolve_local(
            data.arrival_local, data.arrival_timezone, on_gap=GapPolicy.REJECT
        )
        if arrives_at < departs_at:
            raise validation_error("arrival must not be before departure")
    segment.start_date = segment.end_date = None
    segment.departure_local = data.departure_local
    segment.departure_timezone = data.departure_timezone
    segment.departs_at = departs_at
    segment.arrival_local = data.arrival_local
    segment.arrival_timezone = data.arrival_timezone
    segment.arrives_at = arrives_at


async def _segments(ctx: CommandContext, plan_id: UUID) -> list[TravelSegment]:
    rows = await ctx.session.execute(
        select(TravelSegment)
        .where(TravelSegment.plan_id == plan_id, TravelSegment.deleted_at.is_(None))
        .order_by(TravelSegment.sort_order, TravelSegment.id)
    )
    return list(rows.scalars())


async def _segment(ctx: CommandContext, plan_id: UUID, segment_id: UUID) -> TravelSegment:
    segment = (
        await ctx.session.execute(
            select(TravelSegment)
            .where(
                TravelSegment.plan_id == plan_id,
                TravelSegment.id == segment_id,
                TravelSegment.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if segment is None:
        raise not_found()
    return segment


async def _record(
    ctx: CommandContext,
    plan_id: UUID,
    entity_type: str,
    entity_id: UUID,
    version: int,
    action: str,
    *,
    operation: str = "upsert",
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=version,
        scope=ChangeScope.PLAN,
        scope_id=plan_id,
        plan_id=plan_id,
        operation=operation,
    )
