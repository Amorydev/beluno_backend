"""Audit/change recording helpers for plan-scoped entities.

Participant changes say themselves what they mean in the activity feed: joining,
leaving, removal, ownership moves, and a guest upgraded to an account follow from
the action recorded. Leaving, removal, and upgrades count only for rows that were
active, so the feed never reports someone members never saw (a withdrawn join
request is not news). Callers pass an explicit item when they know more (the
previous role, the capabilities before and after, a merge).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from beluno.authorization.policy import AccessState, PlanRole
from beluno.db.models.plans import Plan, PlanInvite, PlanParticipant
from beluno.modules.activity.events import ActivityItem, ActivityType, item
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope, record_change, record_mutation

JOINED_ACTIONS = frozenset(
    {
        "plan_participant.added",
        "plan_participant.joined_via_invite",
        "plan_participant.readded",
        "plan_participant.rejoined_via_invite",
        "plan_participant.approved",
        "plan_participant.claimed",
    }
)


async def record_plan_change(
    ctx: CommandContext,
    plan: Plan,
    action: str,
    metadata: Mapping[str, Any] | None = None,
    activity: ActivityItem | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="plan",
        entity_id=plan.id,
        entity_version=plan.version,
        scope=ChangeScope.PLAN,
        scope_id=plan.id,
        plan_id=plan.id,
        metadata=metadata,
        activity=activity,
    )


def participant_activity(
    participant: PlanParticipant, action: str, previous_state: str | None
) -> ActivityItem | None:
    """What a participant change means in the feed, when the action alone says it."""

    active = participant.access_state == AccessState.ACTIVE.value
    was_active = previous_state == AccessState.ACTIVE.value
    if action in JOINED_ACTIONS and active and participant.role != PlanRole.OWNER.value:
        # The owner arrives with the plan; plan.created says that already.
        return item(
            ActivityType.MEMBER_JOINED, participant_id=participant.id, role=participant.role
        )
    if action == "plan_participant.left" and was_active:
        return item(ActivityType.MEMBER_LEFT, participant_id=participant.id)
    if action == "plan_participant.removed" and was_active:
        return item(ActivityType.MEMBER_REMOVED, participant_id=participant.id)
    if action == "plan_participant.role_changed":
        return item(
            ActivityType.MEMBER_ROLE_CHANGED, participant_id=participant.id, role=participant.role
        )
    if action == "plan_participant.guest_upgraded" and was_active:
        return guest_linked(participant.id, participant.id)
    return None


def guest_linked(participant_id: UUID, guest_participant_id: UUID) -> ActivityItem:
    """A guest's (or placeholder's) row now belongs to ``participant_id``'s account."""

    return item(
        ActivityType.GUEST_LINKED,
        participant_id=participant_id,
        guest_participant_id=guest_participant_id,
    )


async def record_participant_change(
    ctx: CommandContext,
    participant: PlanParticipant,
    action: str,
    metadata: Mapping[str, Any] | None = None,
    activity: ActivityItem | None = None,
    *,
    previous_state: str | None = None,
) -> None:
    """``previous_state`` is the row's access state before this change, when it changed."""

    await record_mutation(
        ctx,
        action=action,
        entity_type="plan_participant",
        entity_id=participant.id,
        entity_version=participant.version,
        scope=ChangeScope.PLAN,
        scope_id=participant.plan_id,
        plan_id=participant.plan_id,
        metadata={
            "role": participant.role,
            "access_state": participant.access_state,
            "rsvp_status": participant.rsvp_status,
            **(metadata or {}),
        },
        activity=activity
        or participant_activity(participant, action, previous_state or participant.access_state),
    )
    if participant.user_id is not None:
        # The person's own user scope learns about their access without reading the plan.
        await record_change(
            ctx,
            entity_type="plan_access",
            entity_id=participant.plan_id,
            entity_version=participant.version,
            scope=ChangeScope.USER,
            scope_id=participant.user_id,
        )


async def record_invite_change(
    ctx: CommandContext,
    invite: PlanInvite,
    action: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="plan_invite",
        entity_id=invite.id,
        entity_version=invite.version,
        scope=ChangeScope.PLAN,
        scope_id=invite.plan_id,
        plan_id=invite.plan_id,
        metadata={"purpose": invite.purpose, "state": invite.state, **(metadata or {})},
    )


def bump(entity: Plan | PlanParticipant | PlanInvite, ctx: CommandContext) -> None:
    entity.version += 1
    entity.updated_at = ctx.now
