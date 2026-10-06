"""Reusable groups: lifecycle, membership, and atomic ownership transfer.

Exactly one active owner exists per group (partial unique index plus a deferred
commit-time trigger). Ownership moves only through ``transfer_ownership``, which
locks the group row so concurrent transfers serialize and exactly one wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import GroupAccess, load_group, require_group
from beluno.authorization.policy import (
    GroupAction,
    GroupRole,
    GroupState,
    MembershipState,
    can_manage_group_member,
)
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    not_found,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.groups import Group, GroupMembership
from beluno.db.models.iam import User
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

# Memberships that still count as being in the group (joined or awaiting an answer).
LIVE_STATES = (MembershipState.ACTIVE.value, MembershipState.INVITED.value)
# Shown when the member's user row is not visible to the caller.
FALLBACK_DISPLAY_NAME = "Beluno member"


@dataclass(frozen=True)
class GroupView:
    group: Group
    membership: GroupMembership | None


@dataclass(frozen=True)
class MemberView:
    membership: GroupMembership
    display_name: str


@dataclass(frozen=True)
class GroupChanges:
    name: str | None = None
    default_currency: str | None = None
    default_timezone: str | None = None


def _require_registered(ctx: CommandContext) -> None:
    if ctx.require_actor().is_guest:
        raise forbidden("Sign in with an account to manage groups")


async def create_group(
    ctx: CommandContext,
    *,
    group_id: UUID | None,
    name: str,
    default_currency: str,
    default_timezone: str,
) -> GroupView:
    _require_registered(ctx)
    actor = ctx.require_actor()
    group = Group(
        id=group_id or new_id(),
        name=name,
        default_currency=default_currency,
        default_timezone=default_timezone,
        state=GroupState.ACTIVE.value,
        deletion_scheduled_at=None,
        created_by_user_id=actor.user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    membership = GroupMembership(
        group_id=group.id,
        user_id=actor.user_id,
        role=GroupRole.OWNER.value,
        state=MembershipState.ACTIVE.value,
        invited_by_user_id=None,
        joined_at=ctx.now,
        left_at=None,
        removed_at=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(group)
            await ctx.session.flush()
            ctx.session.add(membership)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record_group(ctx, group, "group.created")
    await record_group_membership(ctx, membership, "group_membership.joined")
    return GroupView(group=group, membership=membership)


async def get_group(ctx: CommandContext, group_id: UUID) -> GroupView:
    access = await load_group(ctx, group_id)
    require_group(access, GroupAction.VIEW)
    return GroupView(group=access.group, membership=access.membership)


async def list_groups(
    ctx: CommandContext,
    *,
    after_id: UUID | None,
    limit: int,
) -> list[GroupView]:
    actor = ctx.require_actor()
    statement = (
        select(Group, GroupMembership)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(
            GroupMembership.user_id == actor.user_id,
            GroupMembership.state.in_(LIVE_STATES),
        )
        .order_by(Group.id.desc())
        .limit(limit)
    )
    if after_id is not None:
        statement = statement.where(Group.id < after_id)
    rows = await ctx.session.execute(statement)
    return [GroupView(group=group, membership=membership) for group, membership in rows.all()]


async def update_group(
    ctx: CommandContext,
    group_id: UUID,
    expected_version: int,
    changes: GroupChanges,
) -> GroupView:
    access = await _load_for_change(ctx, group_id, GroupAction.UPDATE, expected_version)
    group = access.group
    if changes.name is not None:
        group.name = changes.name
    if changes.default_currency is not None:
        group.default_currency = changes.default_currency
    if changes.default_timezone is not None:
        group.default_timezone = changes.default_timezone
    _bump(group, ctx)
    await ctx.session.flush()
    await _record_group(ctx, group, "group.updated")
    return GroupView(group=group, membership=access.membership)


async def schedule_deletion(
    ctx: CommandContext, group_id: UUID, expected_version: int
) -> GroupView:
    access = await _load_for_change(ctx, group_id, GroupAction.DELETE, expected_version)
    group = access.group
    if group.state == GroupState.DELETION_SCHEDULED.value:
        raise invalid_state("Group deletion is already scheduled")
    group.state = GroupState.DELETION_SCHEDULED.value
    group.deletion_scheduled_at = ctx.now
    _bump(group, ctx)
    await ctx.session.flush()
    await _record_group(ctx, group, "group.deletion_scheduled")
    return GroupView(group=group, membership=access.membership)


async def restore_group(ctx: CommandContext, group_id: UUID, expected_version: int) -> GroupView:
    access = await _load_for_change(ctx, group_id, GroupAction.DELETE, expected_version)
    group = access.group
    if group.state != GroupState.DELETION_SCHEDULED.value:
        raise invalid_state("Group is not scheduled for deletion")
    group.state = GroupState.ACTIVE.value
    group.deletion_scheduled_at = None
    _bump(group, ctx)
    await ctx.session.flush()
    await _record_group(ctx, group, "group.restored")
    return GroupView(group=group, membership=access.membership)


async def list_members(ctx: CommandContext, group_id: UUID) -> list[MemberView]:
    access = await load_group(ctx, group_id)
    require_group(access, GroupAction.VIEW_MEMBERS)
    rows = await ctx.session.execute(
        select(GroupMembership, User.display_name)
        .outerjoin(User, User.id == GroupMembership.user_id)
        .where(
            GroupMembership.group_id == group_id,
            GroupMembership.state.in_(LIVE_STATES),
        )
        .order_by(GroupMembership.created_at, GroupMembership.user_id)
    )
    return [
        MemberView(membership=membership, display_name=name or FALLBACK_DISPLAY_NAME)
        for membership, name in rows.all()
    ]


async def add_member(
    ctx: CommandContext,
    group_id: UUID,
    user_id: UUID,
    role: GroupRole,
) -> MemberView:
    """Invite someone the caller already shares a group or plan with."""

    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, GroupAction.ADD_MEMBER)
    actor_role = _role(access)
    if not can_manage_group_member(actor_role, GroupRole.MEMBER, new_role=role):
        raise forbidden()
    # RLS only exposes users who share a group or plan with the caller.
    target = await ctx.session.get(User, user_id)
    if target is None or target.status != "active":
        raise not_found()
    if target.kind != "registered":
        raise conflict("GUEST_NOT_ALLOWED", "Guests must create an account to join a group")
    membership = await ctx.session.get(GroupMembership, (group_id, user_id), with_for_update=True)
    if membership is None:
        membership = GroupMembership(
            group_id=group_id,
            user_id=user_id,
            role=role.value,
            state=MembershipState.INVITED.value,
            invited_by_user_id=ctx.require_actor().user_id,
            joined_at=None,
            left_at=None,
            removed_at=None,
            version=1,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        ctx.session.add(membership)
    elif membership.state in LIVE_STATES:
        raise conflict("ALREADY_MEMBER", "This person is already in the group")
    else:
        membership.role = role.value
        membership.state = MembershipState.INVITED.value
        membership.invited_by_user_id = ctx.require_actor().user_id
        _bump(membership, ctx)
    await ctx.session.flush()
    await record_group_membership(ctx, membership, "group_membership.invited")
    return MemberView(membership=membership, display_name=target.display_name)


async def answer_invitation(ctx: CommandContext, group_id: UUID, accept: bool) -> GroupView:
    access = await load_group(ctx, group_id)
    require_group(access, GroupAction.RESPOND_INVITATION)
    # Re-read under a row lock: a concurrent removal must win over a late accept.
    membership = (
        await ctx.session.execute(
            select(GroupMembership)
            .where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == ctx.require_actor().user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if membership is None or membership.state != MembershipState.INVITED.value:
        raise invalid_state("This invitation is no longer open")
    if accept:
        membership.state = MembershipState.ACTIVE.value
        membership.joined_at = ctx.now
        action = "group_membership.joined"
    else:
        membership.state = MembershipState.LEFT.value
        membership.left_at = ctx.now
        action = "group_membership.declined"
    _bump(membership, ctx)
    await ctx.session.flush()
    await record_group_membership(ctx, membership, action)
    return GroupView(group=access.group, membership=membership)


async def change_member_role(
    ctx: CommandContext,
    group_id: UUID,
    user_id: UUID,
    role: GroupRole,
    expected_version: int,
) -> MemberView:
    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, GroupAction.CHANGE_MEMBER_ROLE)
    target = await _live_target(ctx, group_id, user_id)
    if target.version != expected_version:
        raise version_conflict(target)
    if not can_manage_group_member(_role(access), GroupRole(target.role), new_role=role):
        raise forbidden()
    target.role = role.value
    _bump(target, ctx)
    await ctx.session.flush()
    await record_group_membership(ctx, target, "group_membership.role_changed")
    return await _member_view(ctx, target)


async def remove_member(ctx: CommandContext, group_id: UUID, user_id: UUID) -> None:
    actor = ctx.require_actor()
    if user_id == actor.user_id:
        await leave_group(ctx, group_id)
        return
    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, GroupAction.REMOVE_MEMBER)
    target = await _live_target(ctx, group_id, user_id)
    if not can_manage_group_member(_role(access), GroupRole(target.role)):
        raise forbidden()
    target.state = MembershipState.REMOVED.value
    target.removed_at = ctx.now
    _bump(target, ctx)
    await ctx.session.flush()
    await record_group_membership(ctx, target, "group_membership.removed")


async def leave_group(ctx: CommandContext, group_id: UUID) -> None:
    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, GroupAction.LEAVE)
    membership = access.membership
    assert membership is not None
    if membership.role == GroupRole.OWNER.value:
        raise conflict(
            "OWNER_TRANSFER_REQUIRED",
            "Transfer ownership before leaving the group",
        )
    membership.state = MembershipState.LEFT.value
    membership.left_at = ctx.now
    _bump(membership, ctx)
    await ctx.session.flush()
    await record_group_membership(ctx, membership, "group_membership.left")


async def transfer_ownership(
    ctx: CommandContext,
    group_id: UUID,
    new_owner_user_id: UUID,
    expected_version: int,
) -> GroupView:
    access = await _load_for_change(ctx, group_id, GroupAction.TRANSFER_OWNERSHIP, expected_version)
    current = access.membership
    assert current is not None
    if new_owner_user_id == current.user_id:
        raise invalid_state("You already own this group")
    target = await _live_target(ctx, group_id, new_owner_user_id)
    if target.state != MembershipState.ACTIVE.value:
        raise invalid_state("The new owner must be an active member")
    # Demote first: the single-owner unique index is checked per statement.
    current.role = GroupRole.ADMIN.value
    _bump(current, ctx)
    await ctx.session.flush()
    target.role = GroupRole.OWNER.value
    _bump(target, ctx)
    _bump(access.group, ctx)
    await ctx.session.flush()
    await _record_group(
        ctx,
        access.group,
        "group.ownership_transferred",
        {"previous_owner_user_id": str(current.user_id), "new_owner_user_id": str(target.user_id)},
    )
    await record_group_membership(ctx, current, "group_membership.role_changed")
    await record_group_membership(ctx, target, "group_membership.role_changed")
    return GroupView(group=access.group, membership=current)


async def _load_for_change(
    ctx: CommandContext,
    group_id: UUID,
    action: GroupAction,
    expected_version: int,
) -> GroupAccess:
    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, action)
    if access.group.version != expected_version:
        raise version_conflict(access.group)
    return access


async def _live_target(ctx: CommandContext, group_id: UUID, user_id: UUID) -> GroupMembership:
    target = await ctx.session.get(GroupMembership, (group_id, user_id), with_for_update=True)
    if target is None or target.state not in LIVE_STATES:
        raise not_found()
    return target


async def _member_view(ctx: CommandContext, membership: GroupMembership) -> MemberView:
    user = await ctx.session.get(User, membership.user_id)
    return MemberView(
        membership=membership, display_name=user.display_name if user else FALLBACK_DISPLAY_NAME
    )


def _role(access: GroupAccess) -> GroupRole:
    assert access.role is not None
    return access.role


def _bump(entity: Group | GroupMembership, ctx: CommandContext) -> None:
    entity.version += 1
    entity.updated_at = ctx.now


async def _record_group(
    ctx: CommandContext,
    group: Group,
    action: str,
    metadata: dict[str, str] | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="group",
        entity_id=group.id,
        entity_version=group.version,
        scope=ChangeScope.GROUP,
        scope_id=group.id,
        group_id=group.id,
        metadata={"state": group.state, **(metadata or {})},
    )


async def record_group_membership(
    ctx: CommandContext, membership: GroupMembership, action: str
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="group_membership",
        entity_id=membership.user_id,
        entity_version=membership.version,
        scope=ChangeScope.GROUP,
        scope_id=membership.group_id,
        group_id=membership.group_id,
        metadata={
            "subject_user_id": str(membership.user_id),
            "role": membership.role,
            "state": membership.state,
        },
    )
