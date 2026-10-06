"""Load the caller's current relationship to a group or plan and enforce a decision.

Every protected endpoint goes through ``load_group``/``load_plan`` plus
``require_group``/``require_plan``. Lookups run under RLS, so a resource the caller
cannot see is indistinguishable from one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from sqlalchemy import Select, select

from beluno.authorization.policy import (
    AccessState,
    Decision,
    GroupAction,
    GroupRole,
    GroupState,
    GroupSubject,
    MembershipState,
    PlanAction,
    PlanRole,
    PlanState,
    PlanSubject,
    Visibility,
    decide_group,
    decide_plan,
)
from beluno.contracts.errors import forbidden, not_found, step_up_required
from beluno.db.models.base import Base
from beluno.db.models.groups import Group, GroupMembership
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.modules.context import CommandContext


@dataclass(frozen=True)
class GroupAccess:
    group: Group
    membership: GroupMembership | None
    subject: GroupSubject

    @property
    def role(self) -> GroupRole | None:
        return self.subject.role


@dataclass(frozen=True)
class PlanAccess:
    plan: Plan
    participant: PlanParticipant | None
    subject: PlanSubject

    @property
    def role(self) -> PlanRole | None:
        return self.subject.participant_role


RowT = TypeVar("RowT", bound=Base)


async def _select_visible(
    ctx: CommandContext,
    statement: Select[RowT],
    for_update: bool,
) -> RowT | None:
    """Lock the row when the caller may write it; otherwise read it unlocked.

    ``SELECT ... FOR UPDATE`` also applies the RLS update policy, which hides rows
    the caller can only read. Falling back to a plain read keeps "visible but not
    allowed" (403) distinct from "not visible" (404); the policy decision that
    follows still denies every write such a caller attempts.
    """

    if for_update:
        locked = (
            await ctx.session.execute(
                statement.with_for_update().execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked is not None:
            return locked
    return (await ctx.session.execute(statement)).scalar_one_or_none()


def enforce(decision: Decision) -> None:
    if decision is Decision.ALLOW:
        return
    if decision is Decision.HIDDEN:
        raise not_found()
    if decision is Decision.STEP_UP_REQUIRED:
        raise step_up_required()
    raise forbidden()


async def load_group(
    ctx: CommandContext,
    group_id: UUID,
    *,
    for_update: bool = False,
) -> GroupAccess:
    actor = ctx.require_actor()
    group = await _select_visible(ctx, select(Group).where(Group.id == group_id), for_update)
    if group is None:
        raise not_found()
    membership = await ctx.session.get(GroupMembership, (group_id, actor.user_id))
    subject = GroupSubject(
        role=GroupRole(membership.role) if membership else None,
        membership_state=MembershipState(membership.state) if membership else None,
        group_state=GroupState(group.state),
        actor_is_guest=actor.is_guest,
        step_up_fresh=ctx.step_up_is_fresh,
    )
    return GroupAccess(group=group, membership=membership, subject=subject)


def require_group(access: GroupAccess, action: GroupAction) -> None:
    enforce(decide_group(action, access.subject))


async def load_plan(
    ctx: CommandContext,
    plan_id: UUID,
    *,
    for_update: bool = False,
) -> PlanAccess:
    actor = ctx.require_actor()
    plan = await _select_visible(ctx, select(Plan).where(Plan.id == plan_id), for_update)
    if plan is None:
        raise not_found()
    participant = await find_user_participant(ctx, plan_id, actor.user_id)
    group_member_active = False
    if plan.group_id is not None:
        membership = await ctx.session.get(GroupMembership, (plan.group_id, actor.user_id))
        group_member_active = membership is not None and membership.state == "active"
    return PlanAccess(
        plan=plan,
        participant=participant,
        subject=PlanSubject(
            participant_role=PlanRole(participant.role) if participant else None,
            access_state=AccessState(participant.access_state) if participant else None,
            group_member_active=group_member_active,
            visibility=Visibility(plan.visibility),
            plan_state=PlanState(plan.state),
            deletion_scheduled=plan.deletion_scheduled_at is not None,
            actor_is_guest=actor.is_guest,
            step_up_fresh=ctx.step_up_is_fresh,
        ),
    )


def require_plan(access: PlanAccess, action: PlanAction) -> None:
    enforce(decide_plan(action, access.subject))


async def find_user_participant(
    ctx: CommandContext,
    plan_id: UUID,
    user_id: UUID,
    *,
    for_update: bool = False,
) -> PlanParticipant | None:
    """The user's live (non-merged) participant row in a plan, if any."""

    statement = select(PlanParticipant).where(
        PlanParticipant.plan_id == plan_id,
        PlanParticipant.user_id == user_id,
        PlanParticipant.access_state != AccessState.MERGED.value,
    )
    if for_update:
        statement = statement.with_for_update()
    return (await ctx.session.execute(statement)).scalar_one_or_none()
