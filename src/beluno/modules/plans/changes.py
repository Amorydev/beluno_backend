"""Audit/change recording helpers for plan-scoped entities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beluno.db.models.plans import Plan, PlanInvite, PlanParticipant, PlanSeries
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope, record_change, record_mutation


async def record_plan_change(
    ctx: CommandContext,
    plan: Plan,
    action: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="plan",
        entity_id=plan.id,
        entity_version=plan.version,
        scope=ChangeScope.PLAN,
        scope_id=plan.id,
        group_id=plan.group_id,
        plan_id=plan.id,
        metadata=metadata,
    )


async def record_participant_change(
    ctx: CommandContext,
    participant: PlanParticipant,
    action: str,
    metadata: Mapping[str, Any] | None = None,
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


def bump(entity: Plan | PlanParticipant | PlanInvite | PlanSeries, ctx: CommandContext) -> None:
    entity.version += 1
    entity.updated_at = ctx.now
