"""Generic plans: creation with participant snapshots, updates, lifecycle, deletion.

``kind`` only selects presentation defaults; no field required here is
travel-specific. Plans copy group defaults at creation, so later group edits
never silently change existing plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import (
    PlanAccess,
    load_group,
    load_plan,
    require_group,
    require_plan,
)
from beluno.authorization.policy import (
    AccessState,
    GroupAction,
    MembershipState,
    PlanAction,
    PlanRole,
    PlanState,
    Visibility,
)
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.groups import Group, GroupMembership
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.plans.changes import bump, record_participant_change, record_plan_change
from beluno.modules.plans.participants import Seed, add_seeded_participant, build_participant
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


@dataclass(frozen=True)
class PlanDraft:
    plan_id: UUID | None
    group_id: UUID | None
    title: str
    kind: str
    state: PlanState
    timing: Timing
    base_currency: str | None
    visibility: Visibility | None
    description: str | None
    location_label: str | None
    seeds: tuple[Seed, ...]
    include_all_group_members: bool


@dataclass(frozen=True)
class PlanChanges:
    title: str | None = None
    kind: str | None = None
    timing: Timing | None = None
    base_currency: str | None = None
    visibility: Visibility | None = None
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


async def resolve_group(ctx: CommandContext, group_id: UUID | None) -> Group | None:
    if group_id is None:
        return None
    access = await load_group(ctx, group_id)
    require_group(access, GroupAction.CREATE_PLAN)
    return access.group


def new_plan(
    ctx: CommandContext,
    *,
    plan_id: UUID | None,
    group: Group | None,
    title: str,
    kind: str,
    state: PlanState,
    timing: Timing,
    base_currency: str | None,
    visibility: Visibility | None,
    description: str | None,
    location_label: str | None,
) -> Plan:
    currency = base_currency or (group.default_currency if group else None)
    if currency is None:
        raise validation_error("base_currency is required for a plan without a group")
    chosen_visibility = visibility or (Visibility.GROUP if group else Visibility.PARTICIPANTS)
    if chosen_visibility is Visibility.GROUP and group is None:
        raise validation_error("group visibility requires a group")
    plan = Plan(
        id=plan_id or new_id(),
        group_id=group.id if group else None,
        series_id=None,
        occurrence_key=None,
        is_series_exception=False,
        title=title,
        kind=kind,
        state=state.value,
        base_currency=currency,
        visibility=chosen_visibility.value,
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


async def active_group_member_ids(ctx: CommandContext, group_id: UUID) -> list[UUID]:
    rows = await ctx.session.execute(
        select(GroupMembership.user_id)
        .where(
            GroupMembership.group_id == group_id,
            GroupMembership.state == MembershipState.ACTIVE.value,
        )
        .order_by(GroupMembership.created_at, GroupMembership.user_id)
    )
    return list(rows.scalars())


async def create_plan(ctx: CommandContext, draft: PlanDraft) -> PlanView:
    require_registered(ctx)
    group = await resolve_group(ctx, draft.group_id)
    plan = new_plan(
        ctx,
        plan_id=draft.plan_id,
        group=group,
        title=draft.title,
        kind=draft.kind,
        state=draft.state,
        timing=draft.timing,
        base_currency=draft.base_currency,
        visibility=draft.visibility,
        description=draft.description,
        location_label=draft.location_label,
    )
    owner = await insert_plan_with_owner(ctx, plan)
    await record_plan_change(ctx, plan, "plan.created", {"kind": plan.kind})
    await record_participant_change(ctx, owner, "plan_participant.added")
    seeds = list(draft.seeds)
    seeded_user_ids = [seed.user_id for seed in seeds if seed.user_id is not None]
    seeded_users = set(seeded_user_ids)
    if len(seeded_users) != len(seeded_user_ids):
        raise validation_error("participants must not repeat a user")
    if draft.include_all_group_members and group is not None:
        for user_id in await active_group_member_ids(ctx, group.id):
            if user_id != owner.user_id and user_id not in seeded_users:
                seeds.append(Seed(user_id=user_id, placeholder_name=None, role=PlanRole.MEMBER))
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
    group_id: UUID | None,
    after_id: UUID | None,
    limit: int,
) -> list[PlanView]:
    actor = ctx.require_actor()
    if group_id is not None:
        access = await load_group(ctx, group_id)
        require_group(access, GroupAction.VIEW)
        # RLS limits the rows to plans this member can see in the group.
        statement = (
            select(Plan, PlanParticipant)
            .outerjoin(
                PlanParticipant,
                (PlanParticipant.plan_id == Plan.id)
                & (PlanParticipant.user_id == actor.user_id)
                & (PlanParticipant.access_state == AccessState.ACTIVE.value),
            )
            .where(Plan.group_id == group_id)
        )
    else:
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
    if changes.kind is not None:
        plan.kind = changes.kind
    if changes.timing is not None:
        apply_timing(plan, changes.timing)
    if changes.base_currency is not None:
        plan.base_currency = changes.base_currency
    if changes.visibility is not None:
        if changes.visibility is Visibility.GROUP and plan.group_id is None:
            raise validation_error("group visibility requires a group")
        plan.visibility = changes.visibility.value
    if changes.description is not UNSET:
        plan.description = changes.description
    if changes.location_label is not UNSET:
        plan.location_label = changes.location_label
    if plan.series_id is not None:
        # An individually edited occurrence is kept out of later series edits.
        plan.is_series_exception = True
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
    if plan.series_id is not None:
        plan.is_series_exception = True
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
