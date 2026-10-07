"""The trip itinerary: items on a day (or anytime), in an order the group chooses.

An item may carry a local start time in an IANA zone (stored as the local time
and zone, never as a fake UTC instant), a place, a lead, who is going, and an
estimated cost. The cost is a finance commitment (``itinerary_item``,
``estimate``) recorded through ``CostCommitmentPort``; the expense that pays it
names that commitment, so budgets count it once. Cancelling or deleting the item
cancels a cost that is still only planned; one already paid stays an expense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess
from beluno.authorization.policy import AccessState, PlanAction
from beluno.contracts.errors import (
    conflict,
    feature_disabled,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.finance import CostCommitment
from beluno.db.models.plans import PlanParticipant
from beluno.db.models.schedule_places import ItemAttendance, ItineraryItem, Place
from beluno.modules.activity.events import ActivityItem, ActivityType, item
from beluno.modules.context import CommandContext
from beluno.modules.finance.commitments import COST_COMMITMENTS, CommitmentDraft
from beluno.modules.finance.states import LINKABLE_COMMITMENT_STATES, CommitmentState
from beluno.modules.planning.common import (
    own_participant_id,
    planning_access,
    record_planning_change,
    require_author_or_manager,
)
from beluno.modules.plans.timing import resolve_local
from beluno.sync.ordering import key_between

ITEM_ENTITY = "itinerary_item"
SHORTLIST, IN_PLAN, POLL_WINNER = "shortlist", "in_plan", "poll_winner"
DEFAULT_COST_CATEGORY = "activities"
COST_SOURCE = "itinerary_item"
COST_KIND = "estimate"
PLANNED, DONE, CANCELLED = "planned", "done", "cancelled"
LINKABLE = frozenset(state.value for state in LINKABLE_COMMITMENT_STATES)


@dataclass(frozen=True)
class EstimatedCost:
    currency: str
    amount_minor: int
    category: str | None = None  # None keeps the current one (or "activities" for a new cost)


@dataclass(frozen=True)
class ItemDraft:
    title: str
    day: date | None = None
    start_time: time | None = None
    timezone: str | None = None
    duration_minutes: int | None = None
    note: str | None = None
    place_id: UUID | None = None
    lead_participant_id: UUID | None = None
    status: str = PLANNED
    order_key: str | None = None
    estimated_cost: EstimatedCost | None = None


@dataclass(frozen=True)
class ItemView:
    item: ItineraryItem
    attendance: list[ItemAttendance] = field(default_factory=list)
    commitment_id: UUID | None = None


async def list_items(ctx: CommandContext, plan_id: UUID) -> list[ItemView]:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(ItineraryItem)
        .where(ItineraryItem.plan_id == plan_id, ItineraryItem.deleted_at.is_(None))
        .order_by(ItineraryItem.day.asc().nulls_last(), ItineraryItem.order_key, ItineraryItem.id)
    )
    return await item_views(ctx, list(rows.scalars()))


async def get_item(ctx: CommandContext, plan_id: UUID, item_id: UUID) -> ItemView:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    return (await item_views(ctx, [await _find(ctx, plan_id, item_id)]))[0]


async def create_item(
    ctx: CommandContext,
    plan_id: UUID,
    item_id: UUID | None,
    draft: ItemDraft,
    *,
    announce: bool = True,
) -> ItemView:
    """``announce=False`` leaves the feed to the caller (adding a place says it once)."""

    # The plan row is locked: the cost commitment needs it, and it orders appends.
    access = await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING, for_update=True)
    entry = ItineraryItem(
        id=item_id or new_id(),
        plan_id=plan_id,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    await _apply(ctx, access, entry, draft, moved=True)
    try:
        async with ctx.savepoint():
            ctx.session.add(entry)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    commitment_id = await _record_cost(ctx, access, entry, draft.estimated_cost)
    await sync_place_status(ctx, plan_id, entry.place_id)
    await _record(
        ctx,
        entry,
        "planning.item_added",
        item(
            ActivityType.ITINERARY_ITEM_ADDED,
            day=entry.day.isoformat() if entry.day else None,
        )
        if announce
        else None,
    )
    return ItemView(entry, [], commitment_id)


async def update_item(
    ctx: CommandContext,
    plan_id: UUID,
    item_id: UUID,
    expected_version: int,
    draft: ItemDraft,
) -> ItemView:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW, for_update=True)
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    await require_author_or_manager(ctx, access, entry.created_by_user_id)
    if entry.version != expected_version:
        raise version_conflict(entry)
    was_done, old_place = entry.status == DONE, entry.place_id
    await _apply(ctx, access, entry, draft, moved=draft.day != entry.day)
    # The item's own write comes first: the finance port flushes the session, and the
    # write guard accepts the changed row only together with its next version.
    _advance(ctx, entry)
    await ctx.session.flush()
    cost = draft.estimated_cost if entry.status != CANCELLED else None
    await _record_cost(ctx, access, entry, cost)
    for place_id in {old_place, entry.place_id}:
        await sync_place_status(ctx, plan_id, place_id)
    done = entry.status == DONE and not was_done
    await _record(
        ctx,
        entry,
        "planning.item_updated",
        item(ActivityType.ITINERARY_ITEM_DONE) if done else None,
    )
    return (await item_views(ctx, [entry]))[0]


async def delete_item(ctx: CommandContext, plan_id: UUID, item_id: UUID) -> None:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW, for_update=True)
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    await require_author_or_manager(ctx, access, entry.created_by_user_id)
    await _record_cost(ctx, access, entry, None)
    entry.deleted_at = ctx.now
    await _bump(ctx, entry, "planning.item_deleted", operation="delete")
    await sync_place_status(ctx, plan_id, entry.place_id)


async def attend(ctx: CommandContext, plan_id: UUID, item_id: UUID, status: str) -> ItemView:
    """Set the caller's own "going / not going"; the same answer again changes nothing."""

    access = await planning_access(ctx, plan_id, PlanAction.RESPOND_PLANNING)
    entry = await _find(ctx, plan_id, item_id, for_update=True)
    participant_id = own_participant_id(access)
    answer = await ctx.session.get(ItemAttendance, (entry.id, participant_id))
    if answer is None:
        ctx.session.add(
            ItemAttendance(
                item_id=entry.id,
                participant_id=participant_id,
                plan_id=plan_id,
                status=status,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
    elif answer.status == status:
        return (await item_views(ctx, [entry]))[0]
    else:
        answer.status = status
        answer.updated_at = ctx.now
    await _bump(ctx, entry, "planning.item_attended")
    return (await item_views(ctx, [entry]))[0]


async def item_views(ctx: CommandContext, items: list[ItineraryItem]) -> list[ItemView]:
    if not items:
        return []
    ids = [entry.id for entry in items]
    # Answers of people who left or were removed no longer count.
    answers = await ctx.session.execute(
        select(ItemAttendance)
        .join(PlanParticipant, PlanParticipant.id == ItemAttendance.participant_id)
        .where(
            ItemAttendance.item_id.in_(ids),
            PlanParticipant.access_state == AccessState.ACTIVE.value,
        )
        .order_by(ItemAttendance.participant_id)
    )
    by_item: dict[UUID, list[ItemAttendance]] = {}
    for answer in answers.scalars():
        by_item.setdefault(answer.item_id, []).append(answer)
    costs = await COST_COMMITMENTS.live_for_sources(
        ctx,
        items[0].plan_id,
        source_type=COST_SOURCE,
        source_ids=ids,
        commitment_kind=COST_KIND,
    )
    return [ItemView(entry, by_item.get(entry.id, []), costs.get(entry.id)) for entry in items]


async def _apply(
    ctx: CommandContext,
    access: PlanAccess,
    entry: ItineraryItem,
    draft: ItemDraft,
    *,
    moved: bool,
) -> None:
    zone = draft.timezone or (access.plan.timezone if draft.start_time else None)
    if draft.start_time is not None:
        if zone is None:
            raise validation_error("timezone is required: the plan has none to default to")
        assert draft.day is not None
        resolve_local(datetime.combine(draft.day, draft.start_time), zone)
    if draft.place_id is not None:
        await _require_place(ctx, entry.plan_id, draft.place_id)
    if draft.lead_participant_id is not None:
        await _require_active_participant(ctx, entry.plan_id, draft.lead_participant_id)
    if draft.order_key is not None:
        entry.order_key = draft.order_key
    elif moved:
        entry.order_key = await _last_key(ctx, entry.plan_id, draft.day)
    entry.day = draft.day
    entry.start_time = draft.start_time
    entry.timezone = zone if draft.start_time is not None else None
    entry.duration_minutes = draft.duration_minutes
    entry.title = draft.title
    entry.note = draft.note
    entry.place_id = draft.place_id
    entry.lead_participant_id = draft.lead_participant_id
    entry.status = draft.status


async def _record_cost(
    ctx: CommandContext, access: PlanAccess, entry: ItineraryItem, cost: EstimatedCost | None
) -> UUID | None:
    """Bring the item's cost commitment in line with ``cost``; write only what changed."""

    current = await _live_cost(ctx, entry)
    if cost is None:
        needed = current is not None and not (
            current.state not in LINKABLE
            and current.converted_from_state == CommitmentState.CANCELLED.value
        )
        if needed:
            _require_finance_writes(ctx)
            await COST_COMMITMENTS.cancel(
                ctx, access, source_type=COST_SOURCE, source_id=entry.id, commitment_kind=COST_KIND
            )
        return None
    category = cost.category or (current.category if current else DEFAULT_COST_CATEGORY)
    if current is not None and (
        current.currency,
        current.amount_minor,
        current.category,
        current.description,
    ) == (cost.currency, cost.amount_minor, category, entry.title):
        return current.id
    _require_finance_writes(ctx)
    commitment = await COST_COMMITMENTS.record(
        ctx,
        access,
        source_type=COST_SOURCE,
        source_id=entry.id,
        commitment_kind=COST_KIND,
        draft=CommitmentDraft(
            category=category,
            description=entry.title,
            currency=cost.currency,
            amount_minor=cost.amount_minor,
            state=CommitmentState.ESTIMATED,
        ),
    )
    return commitment.id


async def _live_cost(ctx: CommandContext, entry: ItineraryItem) -> CostCommitment | None:
    return await ctx.session.scalar(
        select(CostCommitment).where(
            CostCommitment.plan_id == entry.plan_id,
            CostCommitment.source_type == COST_SOURCE,
            CostCommitment.source_id == entry.id,
            CostCommitment.commitment_kind == COST_KIND,
            CostCommitment.state != CommitmentState.CANCELLED.value,
        )
    )


def _require_finance_writes(ctx: CommandContext) -> None:
    """A cost commitment is a finance write: the finance kill switch stops it too."""

    if not ctx.settings.finance_writes_enabled:
        raise feature_disabled()


async def sync_place_status(ctx: CommandContext, plan_id: UUID, place_id: UUID | None) -> None:
    """A place is ``in_plan`` while a live item uses it, back on the shortlist otherwise.

    A poll winner keeps that status either way.
    """

    if place_id is None:
        return
    place = await ctx.session.scalar(
        select(Place)
        .where(Place.plan_id == plan_id, Place.id == place_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if place is None or place.deleted_at is not None or place.status == POLL_WINNER:
        return
    used = await ctx.session.scalar(
        select(func.count())
        .select_from(ItineraryItem)
        .where(
            ItineraryItem.plan_id == plan_id,
            ItineraryItem.place_id == place_id,
            ItineraryItem.deleted_at.is_(None),
        )
    )
    status = IN_PLAN if used else SHORTLIST
    if place.status == status:
        return
    place.status = status
    place.version += 1
    place.updated_at = ctx.now
    await ctx.session.flush()
    await record_planning_change(
        ctx,
        action="planning.place_status_changed",
        entity_type="place",
        entity_id=place.id,
        entity_version=place.version,
        plan_id=plan_id,
        metadata={"version": place.version, "status": status},
    )


async def _last_key(ctx: CommandContext, plan_id: UUID, day: date | None) -> str:
    """A key after every live item of that day (or of anytime)."""

    same_day = ItineraryItem.day.is_(None) if day is None else ItineraryItem.day == day
    last = await ctx.session.scalar(
        select(func.max(ItineraryItem.order_key)).where(
            ItineraryItem.plan_id == plan_id, same_day, ItineraryItem.deleted_at.is_(None)
        )
    )
    return key_between(last, None)


async def _require_place(ctx: CommandContext, plan_id: UUID, place_id: UUID) -> None:
    place = await ctx.session.scalar(
        select(Place.id).where(
            Place.plan_id == plan_id, Place.id == place_id, Place.deleted_at.is_(None)
        )
    )
    if place is None:
        raise validation_error("place_id does not name a saved place of this plan")


async def _require_active_participant(
    ctx: CommandContext, plan_id: UUID, participant_id: UUID
) -> None:
    found = await ctx.session.scalar(
        select(PlanParticipant.id).where(
            PlanParticipant.plan_id == plan_id,
            PlanParticipant.id == participant_id,
            PlanParticipant.access_state == AccessState.ACTIVE.value,
        )
    )
    if found is None:
        raise validation_error("lead_participant_id does not name an active participant")


async def _find(
    ctx: CommandContext, plan_id: UUID, item_id: UUID, *, for_update: bool = False
) -> ItineraryItem:
    statement = select(ItineraryItem).where(
        ItineraryItem.plan_id == plan_id, ItineraryItem.id == item_id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    entry = (await ctx.session.execute(statement)).scalar_one_or_none()
    if entry is None or entry.deleted_at is not None:
        raise not_found()
    return entry


async def _bump(
    ctx: CommandContext,
    entry: ItineraryItem,
    action: str,
    *,
    operation: str = "upsert",
    activity: ActivityItem | None = None,
) -> None:
    _advance(ctx, entry)
    await ctx.session.flush()
    await _record(ctx, entry, action, activity, operation=operation)


def _advance(ctx: CommandContext, entry: ItineraryItem) -> None:
    entry.version += 1
    entry.updated_at = ctx.now


async def _record(
    ctx: CommandContext,
    entry: ItineraryItem,
    action: str,
    activity: ActivityItem | None = None,
    *,
    operation: str = "upsert",
) -> None:
    await record_planning_change(
        ctx,
        action=action,
        entity_type=ITEM_ENTITY,
        entity_id=entry.id,
        entity_version=entry.version,
        plan_id=entry.plan_id,
        metadata={"version": entry.version, "status": entry.status},
        operation=operation,
        activity=activity,
    )
