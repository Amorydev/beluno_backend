"""Plans: trips and hangouts, creation with participant snapshots, updates, lifecycle.

A plan is a ``trip`` (destinations, budgets, the fund) or a light ``hangout``
(one activity icon, expenses and settling only). The type is fixed at creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess, load_plan, require_plan
from beluno.authorization.policy import AccessState, PlanAction, PlanRole, PlanState
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.finance.currencies import require_supported_currency
from beluno.modules.plans.changes import bump, record_participant_change, record_plan_change
from beluno.modules.plans.participants import (
    AVATAR_COLORS,
    Seed,
    add_seeded_participant,
    build_participant,
)
from beluno.modules.plans.timing import check_transition, require_aware

UNSET: Any = object()


@dataclass(frozen=True)
class Timing:
    mode: str = "undecided"
    start_date: date | None = None
    end_date: date | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None


TRIP = "trip"
HANGOUT = "hangout"
PASS_COLORS = ("indigo", "plum", "sea", "forest", "rust", "slate", "wine", "moss")


@dataclass(frozen=True)
class PlanDraft:
    plan_id: UUID | None
    type: str
    title: str
    activity: str | None
    state: PlanState
    timing: Timing
    base_currency: str | None
    destinations: tuple[dict[str, Any], ...]
    pass_color: str | None
    expected_size: int | None
    description: str | None
    location_label: str | None
    seeds: tuple[Seed, ...]


@dataclass(frozen=True)
class PlanChanges:
    title: str | None = None
    activity: str | None = UNSET
    timing: Timing | None = None
    destinations: tuple[dict[str, Any], ...] | None = None
    pass_color: str | None = None
    expected_size: int | None = UNSET
    description: str | None = UNSET
    location_label: str | None = UNSET


@dataclass(frozen=True)
class PlanView:
    plan: Plan
    participant: PlanParticipant | None


def apply_timing(plan: Plan, timing: Timing) -> None:
    plan.timing_mode = timing.mode
    plan.start_date = timing.start_date
    plan.end_date = timing.end_date
    plan.starts_at = require_aware(timing.starts_at, "starts_at") if timing.starts_at else None
    plan.ends_at = require_aware(timing.ends_at, "ends_at") if timing.ends_at else None
    plan.timezone = timing.timezone


def timing_of(plan: Plan) -> Timing:
    return Timing(
        mode=plan.timing_mode,
        start_date=plan.start_date,
        end_date=plan.end_date,
        starts_at=plan.starts_at,
        ends_at=plan.ends_at,
        timezone=plan.timezone,
    )


def require_registered(ctx: CommandContext) -> None:
    if ctx.require_actor().is_guest:
        raise forbidden("Sign in with an account to create plans")


def check_shape(plan_type: str, activity: str | None, destinations: object) -> None:
    """Trips carry destinations; hangouts carry an activity icon. Never the other way."""

    if plan_type == TRIP and activity is not None:
        raise validation_error("activity is for hangouts")
    if plan_type == HANGOUT and destinations:
        raise validation_error("destinations are for trips")


def default_pass_color(plan_id: UUID) -> str:
    return PASS_COLORS[plan_id.int % len(PASS_COLORS)]


def new_plan(
    ctx: CommandContext,
    *,
    plan_id: UUID | None,
    plan_type: str,
    title: str,
    activity: str | None,
    state: PlanState,
    timing: Timing,
    base_currency: str,
    destinations: tuple[dict[str, Any], ...] = (),
    pass_color: str | None = None,
    expected_size: int | None = None,
    description: str | None,
    location_label: str | None,
) -> Plan:
    check_shape(plan_type, activity, destinations)
    identity = plan_id or new_id()
    plan = Plan(
        id=identity,
        type=plan_type,
        title=title,
        activity=activity,
        state=state.value,
        base_currency=base_currency,
        destinations=list(destinations),
        pass_color=pass_color or default_pass_color(identity),
        expected_size=expected_size,
        description=description,
        location_label=location_label,
        duplicated_from_plan_id=None,
        created_by_user_id=ctx.require_actor().user_id,
        deletion_scheduled_at=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    apply_timing(plan, timing)
    return plan


async def insert_plan_with_owner(ctx: CommandContext, plan: Plan) -> PlanParticipant:
    """Insert a plan and its owner participant (the creator) in one savepoint."""

    actor = ctx.require_actor()
    creator = await ctx.session.get(User, actor.user_id)
    assert creator is not None
    owner = build_participant(
        ctx,
        plan_id=plan.id,
        identity_kind="user",
        user_id=creator.id,
        display_name=creator.display_name,
        role=PlanRole.OWNER,
        avatar_color=AVATAR_COLORS[0],
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(plan)
            await ctx.session.flush()
            ctx.session.add(owner)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    return owner


async def create_plan(ctx: CommandContext, draft: PlanDraft) -> PlanView:
    require_registered(ctx)
    creator = await ctx.session.get(User, ctx.require_actor().user_id)
    assert creator is not None
    currency = draft.base_currency or creator.default_currency
    if currency is None:
        raise validation_error("base_currency is required without a default currency")
    await require_supported_currency(ctx, currency)
    plan = new_plan(
        ctx,
        plan_id=draft.plan_id,
        plan_type=draft.type,
        title=draft.title,
        activity=draft.activity,
        state=draft.state,
        timing=draft.timing,
        base_currency=currency,
        destinations=draft.destinations,
        pass_color=draft.pass_color,
        expected_size=draft.expected_size,
        description=draft.description,
        location_label=draft.location_label,
    )
    owner = await insert_plan_with_owner(ctx, plan)
    await record_plan_change(ctx, plan, "plan.created", {"type": plan.type})
    await record_participant_change(ctx, owner, "plan_participant.added")
    seeds = list(draft.seeds)
    seeded_user_ids = [seed.user_id for seed in seeds if seed.user_id is not None]
    if len(set(seeded_user_ids)) != len(seeded_user_ids):
        raise validation_error("participants must not repeat a user")
    for seed in seeds:
        if seed.user_id == owner.user_id:
            continue
        await add_seeded_participant(ctx, plan, seed)
    return PlanView(plan=plan, participant=owner)


async def get_plan(ctx: CommandContext, plan_id: UUID) -> PlanView:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW)
    return PlanView(plan=access.plan, participant=_live(access.participant))


async def list_plans(
    ctx: CommandContext,
    *,
    after_id: UUID | None,
    limit: int,
) -> list[PlanView]:
    actor = ctx.require_actor()
    statement = (
        select(Plan, PlanParticipant)
        .join(PlanParticipant, PlanParticipant.plan_id == Plan.id)
        .where(
            PlanParticipant.user_id == actor.user_id,
            PlanParticipant.access_state == AccessState.ACTIVE.value,
        )
    )
    statement = statement.order_by(Plan.id.desc()).limit(limit)
    if after_id is not None:
        statement = statement.where(Plan.id < after_id)
    rows = await ctx.session.execute(statement)
    return [PlanView(plan=plan, participant=participant) for plan, participant in rows.all()]


async def update_plan(
    ctx: CommandContext,
    plan_id: UUID,
    expected_version: int,
    changes: PlanChanges,
) -> PlanView:
    access = await _load_for_change(ctx, plan_id, PlanAction.UPDATE, expected_version)
    plan = access.plan
    if changes.title is not None:
        plan.title = changes.title
    if changes.activity is not UNSET:
        plan.activity = changes.activity
    if changes.destinations is not None:
        plan.destinations = list(changes.destinations)
    check_shape(plan.type, plan.activity, plan.destinations)
    if changes.pass_color is not None:
        plan.pass_color = changes.pass_color
    if changes.expected_size is not UNSET:
        plan.expected_size = changes.expected_size
    if changes.timing is not None:
        apply_timing(plan, changes.timing)
    if changes.description is not UNSET:
        plan.description = changes.description
    if changes.location_label is not UNSET:
        plan.location_label = changes.location_label
    bump(plan, ctx)
    await ctx.session.flush()
    await record_plan_change(ctx, plan, "plan.updated")
    return PlanView(plan=plan, participant=access.participant)


async def change_state(
    ctx: CommandContext,
    plan_id: UUID,
    expected_version: int,
    target: PlanState,
) -> PlanView:
    access = await _load_for_change(ctx, plan_id, PlanAction.CHANGE_STATE, expected_version)
    plan = access.plan
    check_transition(PlanState(plan.state), target)
    previous = plan.state
    plan.state = target.value
    bump(plan, ctx)
    await ctx.session.flush()
    await record_plan_change(
        ctx, plan, "plan.state_changed", {"from": previous, "to": target.value}
    )
    return PlanView(plan=plan, participant=access.participant)


async def schedule_deletion(ctx: CommandContext, plan_id: UUID, expected_version: int) -> PlanView:
    access = await _load_for_change(ctx, plan_id, PlanAction.DELETE, expected_version)
    plan = access.plan
    if plan.deletion_scheduled_at is not None:
        raise invalid_state("Plan deletion is already scheduled")
    plan.deletion_scheduled_at = ctx.now
    bump(plan, ctx)
    await ctx.session.flush()
    await record_plan_change(ctx, plan, "plan.deletion_scheduled")
    return PlanView(plan=plan, participant=access.participant)


async def restore_plan(ctx: CommandContext, plan_id: UUID, expected_version: int) -> PlanView:
    access = await _load_for_change(ctx, plan_id, PlanAction.DELETE, expected_version)
    plan = access.plan
    if plan.deletion_scheduled_at is None:
        raise invalid_state("Plan is not scheduled for deletion")
    plan.deletion_scheduled_at = None
    bump(plan, ctx)
    await ctx.session.flush()
    await record_plan_change(ctx, plan, "plan.restored")
    return PlanView(plan=plan, participant=access.participant)


async def _load_for_change(
    ctx: CommandContext,
    plan_id: UUID,
    action: PlanAction,
    expected_version: int,
) -> PlanAccess:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, action)
    if access.plan.version != expected_version:
        raise version_conflict(access.plan)
    return access


def _live(participant: PlanParticipant | None) -> PlanParticipant | None:
    if participant is None or participant.access_state != AccessState.ACTIVE.value:
        return None
    return participant
