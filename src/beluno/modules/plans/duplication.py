"""Duplicate a plan through an explicit, versioned copy manifest.

Copied (allowlist ``plan-copy-v1``): title, kind, base currency, visibility,
optionally description and location, optionally the travel destination/notes,
and optionally a selection of currently active registered participants (roles
reset to member, RSVPs reset).

Never copied: ledger, expenses, settlements, funds, balances, media, bookings
and confirmation codes, travel segments, audit history, invites/tokens, guests,
placeholders, removed/left/pending participants, RSVPs, ownership/admin
entitlements, and series membership. New data classes are excluded unless they
are added to this manifest deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import AccessState, PlanAction, PlanRole, PlanState, Visibility
from beluno.contracts.errors import validation_error
from beluno.db.models.plans import PlanParticipant, TravelPlanDetails
from beluno.modules.context import CommandContext
from beluno.modules.plans.changes import record_participant_change, record_plan_change
from beluno.modules.plans.participants import Seed, add_seeded_participant
from beluno.modules.plans.service import (
    PlanView,
    Timing,
    insert_plan_with_owner,
    new_plan,
    require_registered,
    resolve_group,
)
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

COPY_MANIFEST_VERSION = "plan-copy-v1"


@dataclass(frozen=True)
class DuplicateOptions:
    title: str | None
    use_source_group: bool
    group_id: UUID | None
    timing: Timing
    participant_ids: tuple[UUID, ...] | None
    include_description: bool
    include_location: bool
    include_travel_details: bool


async def duplicate_plan(
    ctx: CommandContext,
    source_id: UUID,
    options: DuplicateOptions,
) -> PlanView:
    require_registered(ctx)
    access = await load_plan(ctx, source_id)
    require_plan(access, PlanAction.DUPLICATE)
    source = access.plan
    target_group_id = source.group_id if options.use_source_group else options.group_id
    group = await resolve_group(ctx, target_group_id)
    visibility = Visibility(source.visibility)
    if group is None:
        visibility = Visibility.PARTICIPANTS
    plan = new_plan(
        ctx,
        plan_id=None,
        group=group,
        title=options.title or source.title,
        kind=source.kind,
        state=PlanState.PLANNING,
        timing=options.timing,
        base_currency=source.base_currency,
        visibility=visibility,
        description=source.description if options.include_description else None,
        location_label=source.location_label if options.include_location else None,
    )
    plan.duplicated_from_plan_id = source.id
    owner = await insert_plan_with_owner(ctx, plan)
    await record_plan_change(
        ctx,
        plan,
        "plan.duplicated",
        {"source_plan_id": str(source.id), "manifest": COPY_MANIFEST_VERSION},
    )
    await record_participant_change(ctx, owner, "plan_participant.added")
    for user_id in await _copyable_people(ctx, source.id, options.participant_ids):
        if user_id != owner.user_id:
            await add_seeded_participant(
                ctx, plan, Seed(user_id=user_id, placeholder_name=None, role=PlanRole.MEMBER)
            )
    if options.include_travel_details:
        await _copy_travel_details(ctx, source.id, plan.id)
    return PlanView(plan=plan, participant=owner)


async def _copyable_people(
    ctx: CommandContext,
    source_id: UUID,
    selected: tuple[UUID, ...] | None,
) -> list[UUID]:
    """User IDs of active registered participants, optionally narrowed to a selection."""

    statement = select(PlanParticipant.id, PlanParticipant.user_id).where(
        PlanParticipant.plan_id == source_id,
        PlanParticipant.identity_kind == "user",
        PlanParticipant.access_state == AccessState.ACTIVE.value,
    )
    rows = list((await ctx.session.execute(statement.order_by(PlanParticipant.id))).all())
    if selected is None:
        return [user_id for _, user_id in rows if user_id is not None]
    eligible = {participant_id: user_id for participant_id, user_id in rows}
    unknown = [participant_id for participant_id in selected if participant_id not in eligible]
    if unknown:
        raise validation_error(
            "participant_ids may only name active registered participants of the source plan"
        )
    return [user_id for participant_id in selected if (user_id := eligible[participant_id])]


async def _copy_travel_details(ctx: CommandContext, source_id: UUID, plan_id: UUID) -> None:
    details = await ctx.session.get(TravelPlanDetails, source_id)
    if details is None:
        return
    ctx.session.add(
        TravelPlanDetails(
            plan_id=plan_id,
            destination_summary=details.destination_summary,
            notes=details.notes,
            version=1,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
    )
    await ctx.session.flush()
    await record_mutation(
        ctx,
        action="travel.details_created",
        entity_type="travel_details",
        entity_id=plan_id,
        entity_version=1,
        scope=ChangeScope.PLAN,
        scope_id=plan_id,
        plan_id=plan_id,
    )
