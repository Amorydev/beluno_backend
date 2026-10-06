"""Plan participants: stable identities, access lifecycle, roles, and RSVP.

Access (``access_state``/``role``) and attendance (``rsvp_status``) are separate:
declining never revokes access, and removal never deletes the participant row,
so every historical reference keeps pointing at the same ID. A person who comes
back after leaving or being removed reuses their original participant.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import (
    PlanAccess,
    find_user_participant,
    load_plan,
    require_plan,
)
from beluno.authorization.policy import (
    PLAN_MANAGERS,
    AccessState,
    PlanAction,
    PlanRole,
    can_manage_participant,
)
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.plans.changes import bump, record_participant_change, record_plan_change

LIVE_STATES = (AccessState.ACTIVE.value, AccessState.PENDING_APPROVAL.value)


@dataclass(frozen=True)
class Seed:
    user_id: UUID | None
    placeholder_name: str | None
    role: PlanRole
    # Client-generated ID for a new participant row; ignored when a row is reused.
    participant_id: UUID | None = None


def build_participant(
    ctx: CommandContext,
    *,
    plan_id: UUID,
    identity_kind: str,
    user_id: UUID | None,
    display_name: str,
    role: PlanRole,
    access_state: AccessState = AccessState.ACTIVE,
    added_by_user_id: UUID | None = None,
    invite_id: UUID | None = None,
    participant_id: UUID | None = None,
) -> PlanParticipant:
    return PlanParticipant(
        id=participant_id or new_id(),
        plan_id=plan_id,
        identity_kind=identity_kind,
        user_id=user_id,
        display_name=display_name,
        role=role.value,
        access_state=access_state.value,
        rsvp_status="invited",
        rsvp_updated_at=None,
        merged_into_participant_id=None,
        joined_via_invite_id=invite_id,
        added_by_user_id=added_by_user_id,
        joined_at=ctx.now if access_state is AccessState.ACTIVE else None,
        left_at=None,
        removed_at=None,
        claimed_at=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )


async def insert_participant(
    ctx: CommandContext,
    participant: PlanParticipant,
    action: str = "plan_participant.added",
) -> PlanParticipant:
    try:
        async with ctx.savepoint():
            ctx.session.add(participant)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await record_participant_change(ctx, participant, action)
    return participant


def reactivate(
    ctx: CommandContext,
    participant: PlanParticipant,
    *,
    role: PlanRole | None,
    access_state: AccessState = AccessState.ACTIVE,
) -> None:
    """Bring a former participant back on the same row (stable ID, kept history)."""

    participant.access_state = access_state.value
    if role is not None and participant.role != PlanRole.OWNER.value:
        participant.role = role.value
    participant.joined_at = ctx.now if access_state is AccessState.ACTIVE else None
    participant.left_at = None
    participant.removed_at = None
    participant.rsvp_status = "invited"
    participant.rsvp_updated_at = None
    bump(participant, ctx)


async def visible_registered_user(ctx: CommandContext, user_id: UUID) -> User:
    """RLS only exposes people who share a plan with the caller."""

    user = await ctx.session.get(User, user_id)
    if user is None or user.status != "active":
        raise not_found()
    if user.kind != "registered":
        raise conflict("GUEST_NOT_ALLOWED", "Guests join plans through an invite link")
    return user


async def add_seeded_participant(
    ctx: CommandContext,
    plan: Plan,
    seed: Seed,
) -> PlanParticipant:
    actor_id = ctx.require_actor().user_id
    if seed.placeholder_name is not None:
        if seed.role not in (PlanRole.MEMBER, PlanRole.VIEWER):
            # A bearer claim link must never hand out management rights.
            raise validation_error("placeholders can only be members or viewers")
        return await insert_participant(
            ctx,
            build_participant(
                ctx,
                plan_id=plan.id,
                identity_kind="placeholder",
                user_id=None,
                display_name=seed.placeholder_name,
                role=seed.role,
                added_by_user_id=actor_id,
                participant_id=seed.participant_id,
            ),
        )
    assert seed.user_id is not None
    user = await visible_registered_user(ctx, seed.user_id)
    existing = await find_user_participant(ctx, plan.id, user.id, for_update=True)
    if existing is not None:
        if existing.access_state in LIVE_STATES:
            raise conflict("ALREADY_PARTICIPANT", "This person is already in the plan")
        reactivate(ctx, existing, role=seed.role)
        await ctx.session.flush()
        await record_participant_change(ctx, existing, "plan_participant.readded")
        return existing
    return await insert_participant(
        ctx,
        build_participant(
            ctx,
            plan_id=plan.id,
            identity_kind="user",
            user_id=user.id,
            display_name=user.display_name,
            role=seed.role,
            added_by_user_id=actor_id,
            participant_id=seed.participant_id,
        ),
    )


async def list_participants(
    ctx: CommandContext,
    plan_id: UUID,
    *,
    include_inactive: bool,
) -> list[PlanParticipant]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_PARTICIPANTS)
    states = [AccessState.ACTIVE.value]
    if access.role in PLAN_MANAGERS:
        states.append(AccessState.PENDING_APPROVAL.value)
    if include_inactive:
        states += [AccessState.LEFT.value, AccessState.REMOVED.value, AccessState.MERGED.value]
    rows = await ctx.session.execute(
        select(PlanParticipant)
        .where(PlanParticipant.plan_id == plan_id, PlanParticipant.access_state.in_(states))
        .order_by(PlanParticipant.id)
    )
    return list(rows.scalars())


async def add_participant(ctx: CommandContext, plan_id: UUID, seed: Seed) -> PlanParticipant:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.ADD_PARTICIPANT)
    if not can_manage_participant(_role(access), PlanRole.MEMBER, new_role=seed.role):
        raise forbidden()
    return await add_seeded_participant(ctx, access.plan, seed)


async def change_role(
    ctx: CommandContext,
    plan_id: UUID,
    participant_id: UUID,
    role: PlanRole,
    expected_version: int,
) -> PlanParticipant:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.CHANGE_PARTICIPANT_ROLE)
    target = await _target(ctx, plan_id, participant_id, states=(AccessState.ACTIVE.value,))
    if target.version != expected_version:
        raise version_conflict(target)
    if not can_manage_participant(_role(access), PlanRole(target.role), new_role=role):
        raise forbidden()
    if target.identity_kind != "user" and role is PlanRole.ADMIN:
        raise forbidden("Only registered participants can administer a plan")
    target.role = role.value
    bump(target, ctx)
    await ctx.session.flush()
    await record_participant_change(ctx, target, "plan_participant.role_changed")
    return target


async def remove_participant(ctx: CommandContext, plan_id: UUID, participant_id: UUID) -> None:
    access = await load_plan(ctx, plan_id, for_update=True)
    if access.participant is not None and access.participant.id == participant_id:
        await _leave(ctx, access)
        return
    require_plan(access, PlanAction.REMOVE_PARTICIPANT)
    target = await _target(ctx, plan_id, participant_id, states=LIVE_STATES)
    if not can_manage_participant(_role(access), PlanRole(target.role)):
        raise forbidden()
    target.access_state = AccessState.REMOVED.value
    target.removed_at = ctx.now
    bump(target, ctx)
    await ctx.session.flush()
    await record_participant_change(ctx, target, "plan_participant.removed")


async def leave_plan(ctx: CommandContext, plan_id: UUID) -> None:
    access = await load_plan(ctx, plan_id, for_update=True)
    await _leave(ctx, access)


async def _leave(ctx: CommandContext, access: PlanAccess) -> None:
    require_plan(access, PlanAction.LEAVE)
    participant = access.participant
    assert participant is not None
    if participant.role == PlanRole.OWNER.value:
        raise conflict("OWNER_TRANSFER_REQUIRED", "Transfer ownership before leaving the plan")
    participant.access_state = AccessState.LEFT.value
    participant.left_at = ctx.now
    bump(participant, ctx)
    await ctx.session.flush()
    await record_participant_change(ctx, participant, "plan_participant.left")


async def review_join_request(
    ctx: CommandContext,
    plan_id: UUID,
    participant_id: UUID,
    approve: bool,
) -> PlanParticipant:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.REVIEW_JOIN_REQUEST)
    target = await _target(
        ctx, plan_id, participant_id, states=(AccessState.PENDING_APPROVAL.value,)
    )
    if approve:
        target.access_state = AccessState.ACTIVE.value
        target.joined_at = ctx.now
        action = "plan_participant.approved"
    else:
        target.access_state = AccessState.REMOVED.value
        target.removed_at = ctx.now
        action = "plan_participant.rejected"
    bump(target, ctx)
    await ctx.session.flush()
    await record_participant_change(ctx, target, action)
    return target


async def respond_rsvp(ctx: CommandContext, plan_id: UUID, status: str) -> PlanParticipant:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.RESPOND_RSVP)
    participant = access.participant
    assert participant is not None
    participant.rsvp_status = status
    participant.rsvp_updated_at = ctx.now
    bump(participant, ctx)
    await ctx.session.flush()
    await record_participant_change(ctx, participant, "plan_participant.rsvp_changed")
    return participant


async def transfer_ownership(
    ctx: CommandContext,
    plan_id: UUID,
    new_owner_participant_id: UUID,
    expected_version: int,
) -> PlanAccess:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.TRANSFER_OWNERSHIP)
    if access.plan.version != expected_version:
        raise version_conflict(access.plan)
    current = access.participant
    assert current is not None
    if new_owner_participant_id == current.id:
        raise invalid_state("You already own this plan")
    target = await _target(
        ctx, plan_id, new_owner_participant_id, states=(AccessState.ACTIVE.value,)
    )
    if target.identity_kind != "user":
        raise invalid_state("The new owner must be a registered participant")
    current.role = PlanRole.ADMIN.value
    bump(current, ctx)
    await ctx.session.flush()
    target.role = PlanRole.OWNER.value
    bump(target, ctx)
    bump(access.plan, ctx)
    await ctx.session.flush()
    await record_plan_change(
        ctx,
        access.plan,
        "plan.ownership_transferred",
        {"previous_owner": str(current.id), "new_owner": str(target.id)},
    )
    await record_participant_change(ctx, current, "plan_participant.role_changed")
    await record_participant_change(ctx, target, "plan_participant.role_changed")
    return access


async def _target(
    ctx: CommandContext,
    plan_id: UUID,
    participant_id: UUID,
    *,
    states: tuple[str, ...],
) -> PlanParticipant:
    target = (
        await ctx.session.execute(
            select(PlanParticipant)
            .where(PlanParticipant.plan_id == plan_id, PlanParticipant.id == participant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None or target.access_state not in states:
        raise not_found()
    return target


def _role(access: PlanAccess) -> PlanRole:
    assert access.role is not None
    return access.role
