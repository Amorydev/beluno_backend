"""Recurring plan series that materialize independent plan occurrences.

Each occurrence is an ordinary plan with its own participants and (later)
ledger; nothing mutable is shared. ``(series_id, occurrence_key)`` is unique, so
creating the same occurrence twice (worker retry, overlapping runs) is a no-op.
Materialization is bounded by the series horizon and a per-run cap.

Exceptions: cancelling or editing one occurrence marks only that plan as an
exception. "This and future" changes split the series: the old rule ends the day
before, untouched future occurrences are cancelled, and a new series continues.
Only the series creator manages a series, because the creator owns every
occurrence it materializes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.policy import (
    AccessState,
    MembershipState,
    PlanRole,
    PlanState,
    Visibility,
)
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.groups import GroupMembership
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan, PlanParticipant, PlanSeries
from beluno.modules.context import CommandContext, Runtime, open_context
from beluno.modules.finance.currencies import require_supported_currency
from beluno.modules.plans.changes import bump, record_participant_change, record_plan_change
from beluno.modules.plans.participants import build_participant
from beluno.modules.plans.recurrence import (
    continuation_rule,
    end_before,
    normalize_rule,
    occurrence_dates,
)
from beluno.modules.plans.service import require_registered, resolve_group
from beluno.modules.plans.timing import GapPolicy, resolve_local
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation
from beluno.observability.setup import logger, safe_extra

UNTOUCHED_STATES = (PlanState.DRAFT.value, PlanState.PLANNING.value)
log = logger("beluno.plans.series")


@dataclass(frozen=True)
class SeriesDraft:
    series_id: UUID | None
    group_id: UUID | None
    title: str
    kind: str
    base_currency: str | None
    visibility: Visibility | None
    description: str | None
    location_label: str | None
    timezone: str
    start_date: date
    local_start_time: time | None
    duration_minutes: int | None
    recurrence_rule: str
    participant_user_ids: tuple[UUID, ...]
    horizon_days: int


@dataclass(frozen=True)
class SeriesChanges:
    title: str | None = None
    local_start_time: time | None = None
    duration_minutes: int | None = None
    recurrence_rule: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class SeriesResult:
    series: PlanSeries
    created_plan_ids: list[UUID]


def local_today(series: PlanSeries, now: datetime) -> date:
    return now.astimezone(ZoneInfo(series.timezone)).date()


async def create_series(ctx: CommandContext, draft: SeriesDraft) -> SeriesResult:
    require_registered(ctx)
    actor = ctx.require_actor()
    group = await resolve_group(ctx, draft.group_id)
    currency = draft.base_currency or (group.default_currency if group else None)
    if currency is None:
        raise validation_error("base_currency is required for a series without a group")
    await require_supported_currency(ctx, currency)
    visibility = draft.visibility or (Visibility.GROUP if group else Visibility.PARTICIPANTS)
    if visibility is Visibility.GROUP and group is None:
        raise validation_error("group visibility requires a group")
    if draft.participant_user_ids and group is None:
        raise validation_error("participant_user_ids requires a group")
    if draft.duration_minutes is not None and draft.local_start_time is None:
        raise validation_error("duration_minutes requires local_start_time")
    series = PlanSeries(
        id=draft.series_id or new_id(),
        group_id=group.id if group else None,
        created_by_user_id=actor.user_id,
        title=draft.title,
        kind=draft.kind,
        base_currency=currency,
        visibility=visibility.value,
        description=draft.description,
        location_label=draft.location_label,
        timezone=draft.timezone,
        start_date=draft.start_date,
        local_start_time=draft.local_start_time,
        duration_minutes=draft.duration_minutes,
        recurrence_rule=normalize_rule(draft.recurrence_rule),
        participant_user_ids=list(dict.fromkeys(draft.participant_user_ids)),
        horizon_days=draft.horizon_days,
        materialized_through=None,
        state="active",
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(series)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record_series(ctx, series, "plan_series.created")
    created = await materialize(ctx, series)
    return SeriesResult(series=series, created_plan_ids=created)


async def get_series(ctx: CommandContext, series_id: UUID) -> PlanSeries:
    ctx.require_actor()
    series = await ctx.session.get(PlanSeries, series_id)
    if series is None:
        raise not_found()
    return series


async def list_occurrences(
    ctx: CommandContext,
    series_id: UUID,
    *,
    from_date: date | None,
    limit: int,
) -> list[tuple[Plan, PlanParticipant | None]]:
    """Occurrences the caller can see (RLS), ordered by local date."""

    series = await get_series(ctx, series_id)
    actor = ctx.require_actor()
    statement = (
        select(Plan, PlanParticipant)
        .outerjoin(
            PlanParticipant,
            (PlanParticipant.plan_id == Plan.id)
            & (PlanParticipant.user_id == actor.user_id)
            & (PlanParticipant.access_state == AccessState.ACTIVE.value),
        )
        .where(Plan.series_id == series.id)
        .order_by(Plan.occurrence_key)
        .limit(limit)
    )
    if from_date is not None:
        statement = statement.where(Plan.occurrence_key >= from_date.isoformat())
    rows = await ctx.session.execute(statement)
    return list(rows.all())


async def cancel_series(ctx: CommandContext, series_id: UUID, expected_version: int) -> PlanSeries:
    series = await _managed_series(ctx, series_id, expected_version)
    if series.state != "active":
        raise invalid_state("Series is already cancelled")
    series.state = "cancelled"
    bump(series, ctx)
    await ctx.session.flush()
    await _record_series(ctx, series, "plan_series.cancelled")
    await _cancel_untouched(ctx, series, from_date=local_today(series, ctx.now))
    return series


async def split_series(
    ctx: CommandContext,
    series_id: UUID,
    expected_version: int,
    *,
    from_date: date,
    changes: SeriesChanges,
) -> SeriesResult:
    """Apply ``changes`` to this and future occurrences starting at ``from_date``."""

    series = await _managed_series(ctx, series_id, expected_version)
    if series.state != "active":
        raise invalid_state("Only an active series can be changed")
    if from_date <= series.start_date or from_date < local_today(series, ctx.now):
        raise validation_error("from_date must be after the series start and not in the past")
    if changes.recurrence_rule:
        successor_rule = normalize_rule(changes.recurrence_rule)
        successor_start = from_date
    else:
        # Anchor on the next real occurrence so weekday/interval cadence is unchanged.
        successor_rule = continuation_rule(
            series.recurrence_rule, start_date=series.start_date, boundary=from_date
        )
        upcoming = occurrence_dates(
            series.recurrence_rule,
            start_date=series.start_date,
            window_start=from_date,
            window_end=from_date + timedelta(days=400),
            limit=1,
        )
        if not upcoming:
            raise validation_error("The series has no occurrences on or after from_date")
        successor_start = upcoming[0]
    series.recurrence_rule = end_before(series.recurrence_rule, from_date)
    bump(series, ctx)
    await ctx.session.flush()
    await _record_series(ctx, series, "plan_series.ended_for_split")
    await _cancel_untouched(ctx, series, from_date=from_date)
    start_time = changes.local_start_time or series.local_start_time
    successor = SeriesDraft(
        series_id=None,
        group_id=series.group_id,
        title=changes.title or series.title,
        kind=series.kind,
        base_currency=series.base_currency,
        visibility=Visibility(series.visibility),
        description=series.description,
        location_label=series.location_label,
        timezone=changes.timezone or series.timezone,
        start_date=successor_start,
        local_start_time=start_time,
        duration_minutes=(
            None if start_time is None else changes.duration_minutes or series.duration_minutes
        ),
        recurrence_rule=successor_rule,
        participant_user_ids=tuple(series.participant_user_ids),
        horizon_days=series.horizon_days,
    )
    return await create_series(ctx, successor)


async def materialize(ctx: CommandContext, series: PlanSeries) -> list[UUID]:
    """Create missing occurrences up to the horizon, acting as the series creator."""

    if series.state != "active":
        return []
    creator_id = series.created_by_user_id
    if series.group_id is not None and not await _is_active_member(
        ctx, series.group_id, creator_id
    ):
        log.info("series paused", **safe_extra(event="series_paused", series_id=str(series.id)))
        return []
    today = local_today(series, ctx.now)
    window_end = today + timedelta(days=series.horizon_days)
    dates = occurrence_dates(
        series.recurrence_rule,
        start_date=series.start_date,
        window_start=max(today, series.start_date),
        window_end=window_end,
    )
    creator = await ctx.session.get(User, creator_id)
    if creator is None:
        return []
    attendee_ids = await _attendees(ctx, series)
    existing_keys = set(
        (
            await ctx.session.execute(
                select(Plan.occurrence_key).where(Plan.series_id == series.id)
            )
        ).scalars()
    )
    created: list[UUID] = []
    for occurrence_date in dates:
        if occurrence_date.isoformat() in existing_keys:
            continue
        plan_id = await _insert_occurrence(ctx, series, occurrence_date)
        if plan_id is None:
            continue
        created.append(plan_id)
        await _seed_occurrence(ctx, series, plan_id, creator, attendee_ids)
    series.materialized_through = max(series.materialized_through or window_end, window_end)
    await ctx.session.flush()
    return created


async def extend_all_series(runtime: Runtime) -> int:
    """Periodic worker entrypoint: one transaction per series, failures isolated."""

    async with open_context(runtime) as ctx:
        rows = await ctx.session.execute(
            select(PlanSeries.id, PlanSeries.created_by_user_id).where(PlanSeries.state == "active")
        )
        targets = list(rows.all())
    created = 0
    for series_id, creator_id in targets:
        try:
            async with open_context(runtime) as ctx:
                ctx.on_behalf_of = creator_id
                await ctx.act_as(creator_id)
                series = await ctx.session.get(PlanSeries, series_id, with_for_update=True)
                if series is not None:
                    created += len(await materialize(ctx, series))
        except Exception:
            log.exception(
                "series materialization failed",
                **safe_extra(event="series_failed", series_id=str(series_id)),
            )
    return created


async def _insert_occurrence(
    ctx: CommandContext,
    series: PlanSeries,
    occurrence_date: date,
) -> UUID | None:
    plan_id = new_id()
    timing: dict[str, object] = {
        "timing_mode": "date",
        "start_date": occurrence_date,
        "end_date": None,
        "starts_at": None,
        "ends_at": None,
        "timezone": series.timezone,
    }
    if series.local_start_time is not None:
        # Wall-clock time is kept across DST; a skipped local hour shifts forward.
        starts_at = resolve_local(
            datetime.combine(occurrence_date, series.local_start_time),
            series.timezone,
            on_gap=GapPolicy.SHIFT_FORWARD,
        )
        ends_at = (
            starts_at + timedelta(minutes=series.duration_minutes)
            if series.duration_minutes
            else None
        )
        timing = {
            "timing_mode": "datetime",
            "start_date": None,
            "end_date": None,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": series.timezone,
        }
    statement = insert(Plan).values(
        id=plan_id,
        group_id=series.group_id,
        series_id=series.id,
        occurrence_key=occurrence_date.isoformat(),
        is_series_exception=False,
        title=series.title,
        kind=series.kind,
        state=PlanState.PLANNING.value,
        base_currency=series.base_currency,
        visibility=series.visibility,
        description=series.description,
        location_label=series.location_label,
        duplicated_from_plan_id=None,
        created_by_user_id=series.created_by_user_id,
        deletion_scheduled_at=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        **timing,
    )
    try:
        # A savepoint keeps a concurrent duplicate from aborting the whole run. ON
        # CONFLICT is not usable here: RLS would require the new row to be readable.
        async with ctx.savepoint():
            await ctx.session.execute(statement)
    except IntegrityError:
        return None
    return plan_id


async def _seed_occurrence(
    ctx: CommandContext,
    series: PlanSeries,
    plan_id: UUID,
    creator: User,
    attendee_ids: list[UUID],
) -> None:
    owner = build_participant(
        ctx,
        plan_id=plan_id,
        identity_kind="user",
        user_id=creator.id,
        display_name=creator.display_name,
        role=PlanRole.OWNER,
    )
    ctx.session.add(owner)
    await ctx.session.flush()
    plan = await ctx.session.get(Plan, plan_id)
    assert plan is not None
    await record_plan_change(ctx, plan, "plan.created", {"series_id": str(series.id)})
    await record_participant_change(ctx, owner, "plan_participant.added")
    for user_id in attendee_ids:
        if user_id == creator.id:
            continue
        user = await ctx.session.get(User, user_id)
        if user is None:
            continue
        participant = build_participant(
            ctx,
            plan_id=plan_id,
            identity_kind="user",
            user_id=user.id,
            display_name=user.display_name,
            role=PlanRole.MEMBER,
            added_by_user_id=creator.id,
        )
        ctx.session.add(participant)
        await ctx.session.flush()
        await record_participant_change(ctx, participant, "plan_participant.added")


async def _attendees(ctx: CommandContext, series: PlanSeries) -> list[UUID]:
    """Requested people who are still active group members right now."""

    if series.group_id is None or not series.participant_user_ids:
        return []
    rows = await ctx.session.execute(
        select(GroupMembership.user_id).where(
            GroupMembership.group_id == series.group_id,
            GroupMembership.user_id.in_(series.participant_user_ids),
            GroupMembership.state == MembershipState.ACTIVE.value,
        )
    )
    active = set(rows.scalars())
    return [user_id for user_id in series.participant_user_ids if user_id in active]


async def _is_active_member(ctx: CommandContext, group_id: UUID, user_id: UUID) -> bool:
    membership = await ctx.session.get(GroupMembership, (group_id, user_id))
    return membership is not None and membership.state == MembershipState.ACTIVE.value


async def _cancel_untouched(ctx: CommandContext, series: PlanSeries, *, from_date: date) -> None:
    """Cancel future occurrences nobody edited that the creator still manages.

    An occurrence whose ownership moved to someone else belongs to its new owner.
    """

    rows = await ctx.session.execute(
        select(Plan)
        .join(
            PlanParticipant,
            (PlanParticipant.plan_id == Plan.id)
            & (PlanParticipant.user_id == series.created_by_user_id)
            & (PlanParticipant.access_state == AccessState.ACTIVE.value)
            & PlanParticipant.role.in_([PlanRole.OWNER.value, PlanRole.ADMIN.value]),
        )
        .where(
            Plan.series_id == series.id,
            Plan.occurrence_key >= from_date.isoformat(),
            Plan.is_series_exception.is_(False),
            Plan.state.in_(UNTOUCHED_STATES),
        )
        .with_for_update(of=Plan)
    )
    for plan in rows.scalars():
        plan.state = PlanState.CANCELLED.value
        bump(plan, ctx)
        await ctx.session.flush()
        await record_plan_change(ctx, plan, "plan.cancelled_by_series")


async def _managed_series(
    ctx: CommandContext,
    series_id: UUID,
    expected_version: int,
) -> PlanSeries:
    actor = ctx.require_actor()
    series = (
        await ctx.session.execute(
            select(PlanSeries).where(PlanSeries.id == series_id).with_for_update()
        )
    ).scalar_one_or_none()
    if series is None:
        raise not_found()
    if series.created_by_user_id != actor.user_id or actor.is_guest:
        raise forbidden("Only the series creator can change it")
    if series.version != expected_version:
        raise version_conflict(series)
    return series


async def _record_series(ctx: CommandContext, series: PlanSeries, action: str) -> None:
    scope, scope_id = (
        (ChangeScope.GROUP, series.group_id)
        if series.group_id is not None
        else (ChangeScope.USER, series.created_by_user_id)
    )
    await record_mutation(
        ctx,
        action=action,
        entity_type="plan_series",
        entity_id=series.id,
        entity_version=series.version,
        scope=scope,
        scope_id=scope_id,
        group_id=series.group_id,
        metadata={"state": series.state},
    )
