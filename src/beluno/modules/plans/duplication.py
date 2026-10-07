"""Duplicate a plan ("Clone members and settings") through a versioned copy manifest.

Copied (allowlist ``plan-copy-v2``): type, title, hangout activity, base
currency, destinations, pass colour, expected size, optionally description and
location, and optionally a selection of currently active registered
participants with their role (an owner becomes an admin), default share,
capabilities, and avatar colour. RSVPs reset; the caller owns the copy.

Never copied: dates (the caller sends new ones), ledger, expenses,
settlements, budgets, the fund, balances, media, bookings and confirmation
codes, audit history, invites/tokens, guests, placeholders, and removed, left,
or pending participants. New data classes are excluded unless they are added to
this manifest deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import (
    CAPABILITY_ROLES,
    AccessState,
    PlanAction,
    PlanRole,
    PlanState,
)
from beluno.contracts.errors import validation_error
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.plans.changes import record_participant_change, record_plan_change
from beluno.modules.plans.participants import Seed, add_seeded_participant
from beluno.modules.plans.service import (
    PlanView,
    Timing,
    insert_plan_with_owner,
    new_plan,
    require_registered,
)

COPY_MANIFEST_VERSION = "plan-copy-v2"


@dataclass(frozen=True)
class DuplicateOptions:
    title: str | None
    timing: Timing
    participant_ids: tuple[UUID, ...] | None
    include_description: bool
    include_location: bool


async def duplicate_plan(
    ctx: CommandContext,
    source_id: UUID,
    options: DuplicateOptions,
) -> PlanView:
    require_registered(ctx)
    access = await load_plan(ctx, source_id)
    require_plan(access, PlanAction.DUPLICATE)
    source = access.plan
    plan = new_plan(
        ctx,
        plan_id=None,
        plan_type=source.type,
        title=options.title or source.title,
        activity=source.activity,
        state=PlanState.PLANNING,
        timing=options.timing,
        base_currency=source.base_currency,
        destinations=tuple(source.destinations),
        pass_color=source.pass_color,
        expected_size=source.expected_size,
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
    for person in await _copyable_people(ctx, source.id, options.participant_ids):
        if person.user_id == owner.user_id:
            _copy_settings(person, owner)
            continue
        role = PlanRole.ADMIN if person.role == PlanRole.OWNER.value else PlanRole(person.role)
        copied = await add_seeded_participant(
            ctx, plan, Seed(user_id=person.user_id, placeholder_name=None, role=role)
        )
        _copy_settings(person, copied)
    await ctx.session.flush()
    return PlanView(plan=plan, participant=owner)


def _copy_settings(source: PlanParticipant, target: PlanParticipant) -> None:
    """Settings ride along with the new row's first version (no extra change)."""

    target.default_share = source.default_share
    target.avatar_color = source.avatar_color
    if PlanRole(target.role) in CAPABILITY_ROLES:
        target.capabilities = list(source.capabilities)


async def _copyable_people(
    ctx: CommandContext,
    source_id: UUID,
    selected: tuple[UUID, ...] | None,
) -> list[PlanParticipant]:
    """Active registered participants, optionally narrowed to a selection."""

    statement = select(PlanParticipant).where(
        PlanParticipant.plan_id == source_id,
        PlanParticipant.identity_kind == "user",
        PlanParticipant.access_state == AccessState.ACTIVE.value,
    )
    rows = list((await ctx.session.execute(statement.order_by(PlanParticipant.id))).scalars())
    if selected is None:
        return rows
    eligible = {row.id: row for row in rows}
    unknown = [participant_id for participant_id in selected if participant_id not in eligible]
    if unknown:
        raise validation_error(
            "participant_ids may only name active registered participants of the source plan"
        )
    return [eligible[participant_id] for participant_id in selected]
