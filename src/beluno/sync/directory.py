"""The directory of scopes a caller may sync, with each scope's current head.

The directory is recomputed from current relationships on every handshake, so
a scope that disappears from it is the revocation signal: the client purges
that scope's local data. Membership checks run under RLS on top of the explicit
relationship queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.policy import (
    AccessState,
    GroupRole,
    MembershipState,
    PlanRole,
    Visibility,
)
from beluno.db.models.groups import GroupMembership
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.db.models.sync_audit import ScopeHead
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope
from beluno.sync.scopes import AccessLevel, ScopeKey

DIRECTORY_PAGE_SIZE = 500


@dataclass(frozen=True)
class DirectoryEntry:
    scope: ScopeKey
    level: AccessLevel
    head: int
    floor: int
    generation: int


async def visible_scopes(ctx: CommandContext) -> dict[ScopeKey, AccessLevel]:
    """Every scope the caller may read, from their current memberships and participations."""

    actor = ctx.require_actor()
    scopes: dict[ScopeKey, AccessLevel] = {
        ScopeKey(ChangeScope.USER, actor.user_id): AccessLevel.SELF
    }
    memberships = (
        await ctx.session.execute(
            select(GroupMembership.group_id, GroupMembership.role, GroupMembership.state).where(
                GroupMembership.user_id == actor.user_id,
                GroupMembership.state.in_(
                    (MembershipState.ACTIVE.value, MembershipState.INVITED.value)
                ),
            )
        )
    ).all()
    active_groups: list[UUID] = []
    for group_id, role, state in memberships:
        if state == MembershipState.INVITED.value:
            level = AccessLevel.INVITED
        else:
            active_groups.append(group_id)
            manager = role in (GroupRole.OWNER.value, GroupRole.ADMIN.value)
            level = AccessLevel.MANAGER if manager else AccessLevel.MEMBER
        scopes[ScopeKey(ChangeScope.GROUP, group_id)] = level
    if active_groups:
        group_plans = (
            await ctx.session.execute(
                select(Plan.id).where(
                    Plan.group_id.in_(active_groups),
                    Plan.visibility == Visibility.GROUP.value,
                )
            )
        ).scalars()
        for plan_id in group_plans:
            scopes[ScopeKey(ChangeScope.PLAN, plan_id)] = AccessLevel.READER
    participations = (
        await ctx.session.execute(
            select(PlanParticipant.plan_id, PlanParticipant.role).where(
                PlanParticipant.user_id == actor.user_id,
                PlanParticipant.access_state == AccessState.ACTIVE.value,
            )
        )
    ).all()
    for plan_id, role in participations:
        manager = role in (PlanRole.OWNER.value, PlanRole.ADMIN.value)
        scopes[ScopeKey(ChangeScope.PLAN, plan_id)] = (
            AccessLevel.MANAGER if manager else AccessLevel.MEMBER
        )
    return scopes


async def load_heads(ctx: CommandContext, scopes: list[ScopeKey]) -> dict[ScopeKey, ScopeHead]:
    if not scopes:
        return {}
    rows = (
        await ctx.session.execute(
            select(ScopeHead).where(
                ScopeHead.scope_id.in_([scope.scope_id for scope in scopes]),
                ScopeHead.scope_type.in_({scope.scope_type.value for scope in scopes}),
            )
        )
    ).scalars()
    return {ScopeKey(ChangeScope(row.scope_type), row.scope_id): row for row in rows}


async def directory_page(
    ctx: CommandContext,
    *,
    after: ScopeKey | None,
    limit: int | None = None,
) -> tuple[list[DirectoryEntry], ScopeKey | None]:
    """A page of the directory ordered by scope key; returns the next page's start."""

    page_size = limit or DIRECTORY_PAGE_SIZE
    levels = await visible_scopes(ctx)
    ordered = sorted(levels, key=lambda scope: scope.sort_key)
    if after is not None:
        ordered = [scope for scope in ordered if scope.sort_key > after.sort_key]
    page, rest = ordered[:page_size], ordered[page_size:]
    heads = await load_heads(ctx, page)
    entries = []
    for scope in page:
        head = heads.get(scope)
        entries.append(
            DirectoryEntry(
                scope=scope,
                level=levels[scope],
                head=head.last_seq if head else 0,
                floor=head.floor_seq if head else 0,
                generation=head.generation if head else 1,
            )
        )
    return entries, (page[-1] if rest else None)
