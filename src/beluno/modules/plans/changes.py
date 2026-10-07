"""Audit/change recording helpers for plan-scoped entities.

Participant changes say themselves what they mean in the activity feed: joining,
leaving, removal, ownership moves, and a guest linked to an account follow from
the action recorded. Callers pass an explicit item when they know more (the
previous role, the capabilities before and after).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
LINKED_ACTIONS = frozenset({"plan_participant.merged", "plan_participant.guest_upgraded"})


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


def participant_activity(participant: PlanParticipant, action: str) -> ActivityItem | None:
    """What a participant change means in the feed, when the action alone says it."""

    active = participant.access_state == AccessState.ACTIVE.value
    if action in JOINED_ACTIONS and active and participant.role != PlanRole.OWNER.value:
        # The owner arrives with the plan; plan.created says that already.
        return item(
            ActivityType.MEMBER_JOINED, participant_id=participant.id, role=participant.role
        )
    if action == "plan_participant.left":
        return item(ActivityType.MEMBER_LEFT, participant_id=participant.id)
    if action == "plan_participant.removed":
        return item(ActivityType.MEMBER_REMOVED, participant_id=participant.id)
    if action == "plan_participant.role_changed":
        return item(
            ActivityType.MEMBER_ROLE_CHANGED, participant_id=participant.id, role=participant.role
        )
    if action in LINKED_ACTIONS:
        return item(
            ActivityType.GUEST_LINKED,
            participant_id=participant.merged_into_participant_id or participant.id,
            guest_participant_id=participant.id,
        )
    return None


async def record_participant_change(
    ctx: CommandContext,
    participant: PlanParticipant,
    action: str,
    metadata: Mapping[str, Any] | None = None,
    activity: ActivityItem | None = None,
) -> None:
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
        activity=activity or participant_activity(participant, action),
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
