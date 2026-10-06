"""Sync scopes and the caller's current access level to each.

A scope is one independent change stream: ``user:{id}`` or ``plan:{id}``. The
access level decides which entity types a cursor may read; it is bound into
every cursor so a change of level forces a fresh snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from beluno.authorization.policy import AccessState, PlanRole
from beluno.contracts.errors import validation_error
from beluno.db.models.plans import Plan
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope


class AccessLevel(StrEnum):
    SELF = "self"
    MANAGER = "manager"
    MEMBER = "member"


@dataclass(frozen=True)
class ScopeKey:
    scope_type: ChangeScope
    scope_id: UUID

    def __str__(self) -> str:
        return f"{self.scope_type.value}:{self.scope_id}"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Directory order: the user scope first, then plans by ID."""

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
    plan = await ctx.session.get(Plan, scope.scope_id)
    if plan is None:
        return None
    from beluno.authorization.access import find_user_participant

    participant = await find_user_participant(ctx, plan.id, actor.user_id)
    if participant is not None and participant.access_state == AccessState.ACTIVE.value:
        manager = participant.role in (PlanRole.OWNER.value, PlanRole.ADMIN.value)
        return AccessLevel.MANAGER if manager else AccessLevel.MEMBER
    return None
