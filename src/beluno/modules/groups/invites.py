"""Group invite links: the way new people join a reusable group.

Only registered accounts can hold group membership, so guests must sign in
before redeeming. Redemption locks the invite row and counts each new member once.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.access import load_group, require_group
from beluno.authorization.policy import (
    GroupAction,
    GroupRole,
    GroupState,
    MembershipState,
    can_manage_group_member,
)
from beluno.contracts.errors import (
    authentication_failed,
    forbidden,
    invite_unavailable,
    not_found,
)
from beluno.db.ids import new_id
from beluno.db.models.groups import Group, GroupInvite, GroupMembership
from beluno.modules.context import CommandContext
from beluno.modules.groups.service import GroupView, record_group_membership
from beluno.modules.invite_links import expiry, issue_invite_token
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation


@dataclass(frozen=True)
class CreatedGroupInvite:
    invite: GroupInvite
    token: str


async def create_invite(
    ctx: CommandContext,
    group_id: UUID,
    *,
    role: GroupRole,
    max_uses: int | None,
    expires_in_hours: int,
) -> CreatedGroupInvite:
    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, GroupAction.ADD_MEMBER)
    assert access.role is not None
    if not can_manage_group_member(access.role, GroupRole.MEMBER, new_role=role):
        raise forbidden("Only the owner can invite administrators")
    token = issue_invite_token(ctx.runtime.require_hasher())
    invite = GroupInvite(
        id=new_id(),
        group_id=group_id,
        token_hash=token.digest,
        role=role.value,
        max_uses=max_uses,
        use_count=0,
        expires_at=expiry(ctx.now, expires_in_hours),
        state="active",
        revoked_at=None,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    ctx.session.add(invite)
    await ctx.session.flush()
    await _record(ctx, invite, "group_invite.created")
    return CreatedGroupInvite(invite=invite, token=token.raw)


async def list_invites(ctx: CommandContext, group_id: UUID) -> list[GroupInvite]:
    access = await load_group(ctx, group_id)
    require_group(access, GroupAction.ADD_MEMBER)
    rows = await ctx.session.execute(
        select(GroupInvite).where(GroupInvite.group_id == group_id).order_by(GroupInvite.id.desc())
    )
    return list(rows.scalars())


async def revoke_invite(ctx: CommandContext, group_id: UUID, invite_id: UUID) -> GroupInvite:
    access = await load_group(ctx, group_id, for_update=True)
    require_group(access, GroupAction.ADD_MEMBER)
    invite = (
        await ctx.session.execute(
            select(GroupInvite)
            .where(GroupInvite.group_id == group_id, GroupInvite.id == invite_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise not_found()
    if invite.state != "revoked":
        invite.state = "revoked"
        invite.revoked_at = ctx.now
        invite.version += 1
        invite.updated_at = ctx.now
        await ctx.session.flush()
        await _record(ctx, invite, "group_invite.revoked")
    return invite


async def find_by_digest(
    ctx: CommandContext,
    digest: bytes,
    *,
    for_update: bool,
) -> GroupInvite | None:
    statement = select(GroupInvite).where(GroupInvite.token_hash == digest)
    if for_update:
        statement = statement.with_for_update()
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def invited_group(ctx: CommandContext, invite: GroupInvite) -> Group:
    group = await ctx.session.get(Group, invite.group_id)
    if group is None or group.state != GroupState.ACTIVE.value:
        raise invite_unavailable()
    return group


async def redeem(ctx: CommandContext, invite: GroupInvite) -> GroupView:
    """``invite`` must be locked and already checked as usable by the caller."""

    actor = ctx.actor
    if actor is None or actor.is_guest:
        raise authentication_failed()
    group = await invited_group(ctx, invite)
    membership = await ctx.session.get(
        GroupMembership, (group.id, actor.user_id), with_for_update=True
    )
    if membership is not None and membership.state == MembershipState.ACTIVE.value:
        return GroupView(group=group, membership=membership)
    if membership is not None and membership.state == MembershipState.REMOVED.value:
        raise forbidden("You were removed from this group; ask an organizer to add you")
    if membership is None:
        membership = GroupMembership(
            group_id=group.id,
            user_id=actor.user_id,
            role=invite.role,
            state=MembershipState.ACTIVE.value,
            invited_by_user_id=invite.created_by_user_id,
            joined_at=ctx.now,
            left_at=None,
            removed_at=None,
            version=1,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        ctx.session.add(membership)
    else:
        # A pending direct invitation keeps its role; someone who left takes the link's role.
        if membership.state == MembershipState.LEFT.value:
            membership.role = invite.role
        membership.state = MembershipState.ACTIVE.value
        membership.joined_at = ctx.now
        membership.left_at = None
        membership.version += 1
        membership.updated_at = ctx.now
    invite.use_count += 1
    invite.version += 1
    invite.updated_at = ctx.now
    await ctx.session.flush()
    await record_group_membership(ctx, membership, "group_membership.joined_via_invite")
    await _record(ctx, invite, "group_invite.redeemed")
    return GroupView(group=group, membership=membership)


async def _record(ctx: CommandContext, invite: GroupInvite, action: str) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type="group_invite",
        entity_id=invite.id,
        entity_version=invite.version,
        scope=ChangeScope.GROUP,
        scope_id=invite.group_id,
        group_id=invite.group_id,
        metadata={"role": invite.role, "state": invite.state},
    )
