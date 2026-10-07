"""Crews: a user's private, saved list of people ("Hotpot gang", "Japan crew").

A crew is never shared and never carries money: starting a plan from it only
brings the people. Crews belong to registered accounts and list registered
people only: guests join plans through invite links, and a guest account can be
retired by a claim. Anyone listed must be the owner or someone who currently
shares an active plan with the owner (the people the owner can already see).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import AccessState, PlanAction
from beluno.contracts.errors import (
    conflict,
    forbidden,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.iam import User
from beluno.db.models.people import Crew
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

CREW_ENTITY = "crew"


@dataclass(frozen=True)
class CrewMember:
    user_id: UUID
    display_name: str | None
    addable: bool


@dataclass(frozen=True)
class CrewView:
    crew: Crew
    members: list[CrewMember]


# --- reads ---------------------------------------------------------------------------


async def list_crews(ctx: CommandContext) -> list[CrewView]:
    rows = await ctx.session.execute(
        select(Crew)
        .where(Crew.owner_user_id == ctx.require_actor().user_id, Crew.deleted_at.is_(None))
        .order_by(Crew.id)
    )
    return await crew_views(ctx, list(rows.scalars()))


async def get_crew(ctx: CommandContext, crew_id: UUID) -> CrewView:
    return (await crew_views(ctx, [await _own_crew(ctx, crew_id)]))[0]


async def crew_views(ctx: CommandContext, crews: Sequence[Crew]) -> list[CrewView]:
    """Render crews with each person's current name and whether a plan can add them."""

    owner = ctx.require_actor().user_id
    everyone = {user_id for crew in crews for user_id in crew.member_user_ids}
    shared = await shared_people(ctx, everyone)
    users = {
        user.id: user
        for user in (await ctx.session.execute(select(User).where(User.id.in_(everyone)))).scalars()
    }
    views = []
    for crew in crews:
        members = []
        for user_id in crew.member_user_ids:
            user = users.get(user_id)
            members.append(
                CrewMember(
                    user_id=user_id,
                    display_name=user.display_name if user else None,
                    addable=(
                        user is not None
                        and user_id != owner
                        and user_id in shared
                        and user.kind == "registered"
                        and user.status == "active"
                    ),
                )
            )
        views.append(CrewView(crew=crew, members=members))
    return views


async def shared_people(ctx: CommandContext, user_ids: Iterable[UUID]) -> set[UUID]:
    """The given people who are active in an active plan of the caller right now."""

    wanted = list(set(user_ids))
    if not wanted:
        return set()
    mine, theirs = aliased(PlanParticipant), aliased(PlanParticipant)
    rows = await ctx.session.execute(
        select(theirs.user_id)
        .join(mine, mine.plan_id == theirs.plan_id)
        .where(
            mine.user_id == ctx.require_actor().user_id,
            mine.access_state == AccessState.ACTIVE.value,
            theirs.access_state == AccessState.ACTIVE.value,
            theirs.user_id.in_(wanted),
        )
        .distinct()
    )
    return {user_id for user_id in rows.scalars() if user_id is not None}


# --- writes --------------------------------------------------------------------------


async def create_crew(
    ctx: CommandContext,
    crew_id: UUID | None,
    name: str,
    *,
    member_user_ids: Sequence[UUID] | None,
    from_plan_id: UUID | None,
) -> CrewView:
    actor = ctx.require_actor()
    if actor.is_guest:
        raise forbidden("Crews need a registered account")
    owner = actor.user_id
    if from_plan_id is not None:
        members = await _plan_people(ctx, from_plan_id)
    else:
        assert member_user_ids is not None
        members = list(member_user_ids)
        await _require_known(ctx, members)
    crew = Crew(
        id=crew_id or new_id(),
        owner_user_id=owner,
        name=name,
        member_user_ids=members,
        source_plan_id=from_plan_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(crew)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(ctx, crew, "people.crew_created")
    return await get_crew(ctx, crew.id)


async def update_crew(
    ctx: CommandContext,
    crew_id: UUID,
    expected_version: int,
    *,
    name: str | None,
    member_user_ids: Sequence[UUID] | None,
) -> CrewView:
    crew = await _own_crew(ctx, crew_id, for_update=True)
    if crew.version != expected_version:
        raise version_conflict(crew)
    if name is not None:
        crew.name = name
    if member_user_ids is not None:
        # People already listed may stay even after their plans end.
        await _require_known(
            ctx,
            [user_id for user_id in member_user_ids if user_id not in set(crew.member_user_ids)],
        )
        crew.member_user_ids = list(member_user_ids)
    crew.version += 1
    crew.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, crew, "people.crew_updated")
    return await get_crew(ctx, crew.id)


async def delete_crew(ctx: CommandContext, crew_id: UUID) -> None:
    crew = await _own_crew(ctx, crew_id, for_update=True)
    # The tombstone only tells devices the crew is gone; its name and people go now.
    crew.deleted_at = ctx.now
    crew.name = ""
    crew.member_user_ids = []
    crew.version += 1
    crew.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, crew, "people.crew_deleted", operation="delete")


# --- helpers -------------------------------------------------------------------------


async def _plan_people(ctx: CommandContext, plan_id: UUID) -> list[UUID]:
    """Every registered account active in the plan, in join order; guests are left out."""

    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_PARTICIPANTS)
    rows = await ctx.session.execute(
        select(PlanParticipant.user_id)
        .join(User, User.id == PlanParticipant.user_id)
        .where(
            PlanParticipant.plan_id == plan_id,
            PlanParticipant.access_state == AccessState.ACTIVE.value,
            User.kind == "registered",
        )
        .order_by(PlanParticipant.created_at, PlanParticipant.id)
    )
    members = [user_id for user_id in rows.scalars() if user_id is not None]
    if len(members) > 50:
        raise validation_error("a crew holds at most 50 people")
    return members


async def _require_known(ctx: CommandContext, user_ids: Sequence[UUID]) -> None:
    owner = ctx.require_actor().user_id
    others = {user_id for user_id in user_ids if user_id != owner}
    if others - await shared_people(ctx, others):
        raise validation_error("crew members must share an active plan with you")
    registered = await ctx.session.execute(
        select(User.id).where(User.id.in_(others), User.kind == "registered")
    )
    if others - set(registered.scalars()):
        raise validation_error("crews list registered people; guests join through an invite")


async def _own_crew(ctx: CommandContext, crew_id: UUID, *, for_update: bool = False) -> Crew:
    statement = select(Crew).where(
        Crew.id == crew_id,
        Crew.owner_user_id == ctx.require_actor().user_id,
        Crew.deleted_at.is_(None),
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    crew = (await ctx.session.execute(statement)).scalar_one_or_none()
    if crew is None:
        raise not_found()
    return crew


async def _record(
    ctx: CommandContext, crew: Crew, action: str, *, operation: str = "upsert"
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=CREW_ENTITY,
        entity_id=crew.id,
        entity_version=crew.version,
        scope=ChangeScope.USER,
        scope_id=crew.owner_user_id,
        metadata={"version": crew.version, "members": len(crew.member_user_ids)},
        operation=operation,
    )
