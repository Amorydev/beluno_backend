"""Sync scopes and the caller's current access level to each.

A scope is one independent change stream: ``user:{id}``, ``group:{id}``, or
``plan:{id}``. The access level decides which entity types a cursor may read;
it is bound into every cursor so a change of level forces a fresh snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from beluno.authorization.policy import (
    AccessState,
    GroupRole,
    MembershipState,
    PlanRole,
    Visibility,
)
from beluno.contracts.errors import validation_error
from beluno.db.models.groups import Group, GroupMembership
from beluno.db.models.plans import Plan
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope


class AccessLevel(StrEnum):
    SELF = "self"
    MANAGER = "manager"
    MEMBER = "member"
    READER = "reader"
    INVITED = "invited"


@dataclass(frozen=True)
class ScopeKey:
    scope_type: ChangeScope
    scope_id: UUID

    def __str__(self) -> str:
        return f"{self.scope_type.value}:{self.scope_id}"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Directory order: the user scope first, then groups and plans by ID."""

        return (
            0 if self.scope_type is ChangeScope.USER else 1,
            self.scope_type.value,
            str(self.scope_id),
        )

    @classmethod
    def parse(cls, value: str) -> ScopeKey:
        kind, _, raw_id = value.partition(":")
        try:
            return cls(ChangeScope(kind), UUID(raw_id))
        except ValueError as error:
            raise validation_error(f"scope {value!r} is invalid") from error


async def scope_access(ctx: CommandContext, scope: ScopeKey) -> AccessLevel | None:
    """The caller's level for ``scope`` right now, or ``None`` when it is not theirs to read."""

    actor = ctx.require_actor()
    if scope.scope_type is ChangeScope.USER:
        return AccessLevel.SELF if scope.scope_id == actor.user_id else None
    if scope.scope_type is ChangeScope.GROUP:
        if await ctx.session.get(Group, scope.scope_id) is None:
            return None
        membership = await ctx.session.get(GroupMembership, (scope.scope_id, actor.user_id))
        if membership is None:
            return None
        if membership.state == MembershipState.INVITED.value:
            return AccessLevel.INVITED
        if membership.state != MembershipState.ACTIVE.value:
            return None
        manager = membership.role in (GroupRole.OWNER.value, GroupRole.ADMIN.value)
        return AccessLevel.MANAGER if manager else AccessLevel.MEMBER
    plan = await ctx.session.get(Plan, scope.scope_id)
    if plan is None:
        return None
    from beluno.authorization.access import find_user_participant

    participant = await find_user_participant(ctx, plan.id, actor.user_id)
    if participant is not None and participant.access_state == AccessState.ACTIVE.value:
        manager = participant.role in (PlanRole.OWNER.value, PlanRole.ADMIN.value)
        return AccessLevel.MANAGER if manager else AccessLevel.MEMBER
    if plan.group_id is not None and plan.visibility == Visibility.GROUP.value:
        membership = await ctx.session.get(GroupMembership, (plan.group_id, actor.user_id))
        if membership is not None and membership.state == MembershipState.ACTIVE.value:
            return AccessLevel.READER
    return None
