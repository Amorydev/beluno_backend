"""What every trip-planning command shares: access, trip-only, authorship, recording."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from beluno.authorization.access import PlanAccess, load_plan, require_plan
from beluno.authorization.policy import Decision, PlanAction, decide_plan
from beluno.contracts.errors import BelunoError, conflict, forbidden
from beluno.modules.activity.events import ActivityItem
from beluno.modules.context import CommandContext
from beluno.modules.iam.users import is_actor_account
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

TRIP = "trip"


def not_available_for_hangout() -> BelunoError:
    return conflict(
        "NOT_AVAILABLE_FOR_HANGOUT",
        "The trip plan is for trips",
        "A hangout has no itinerary, places, polls, bookings, tasks, or packing",
    )


async def planning_access(
    ctx: CommandContext, plan_id: UUID, action: PlanAction, *, for_update: bool = False
) -> PlanAccess:
    """Authorize ``action`` on a trip (hangouts have no trip plan)."""

    access = await load_plan(ctx, plan_id, for_update=for_update)
    require_plan(access, action)
    if access.plan.type != TRIP:
        raise not_available_for_hangout()
    return access


async def require_author_or_manager(
    ctx: CommandContext, access: PlanAccess, author_id: UUID
) -> None:
    """The author edits their own (while still contributing); managers edit anyone's.

    A guest who later claimed an account still counts as the author of what they added.
    """

    if decide_plan(PlanAction.MANAGE_PLANNING, access.subject) is Decision.ALLOW:
        return
    require_plan(access, PlanAction.CONTRIBUTE_PLANNING)
    if not await is_actor_account(ctx, author_id):
        raise forbidden("Only the person who added it, or an organizer, can change it")


def own_participant_id(access: PlanAccess) -> UUID:
    assert access.participant is not None
    return access.participant.id


async def record_planning_change(
    ctx: CommandContext,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    entity_version: int,
    plan_id: UUID,
    metadata: Mapping[str, Any] | None = None,
    operation: str = "upsert",
    activity: ActivityItem | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=entity_version,
        scope=ChangeScope.PLAN,
        scope_id=plan_id,
        plan_id=plan_id,
        metadata=metadata,
        operation=operation,
        activity=activity,
    )
