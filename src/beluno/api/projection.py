"""Entity projections for the sync feed, rendered with the public presenters.

Visibility mirrors the REST resources: a scope's level decides which entity
types a caller receives at all, and a few row-level rules (pending participants
and invites for managers only, an invited person's own membership) hide rows
without revealing them. Rows that stopped being visible come back as deletes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, select
from sqlalchemy.orm import InstrumentedAttribute

from beluno.api.presenters import (
    group_response,
    invite_response,
    member_response,
    participant_response,
    plan_response,
    profile_response,
    segment_response,
    series_response,
    travel_response,
)
from beluno.authorization.access import find_user_participant
from beluno.authorization.policy import AccessState
from beluno.contracts.iam import SessionResponse
from beluno.contracts.sync import (
    GroupAccessSignal,
    PlanAccessSignal,
    PlanEntity,
    TravelDetailsEntity,
)
from beluno.db.models.base import Base
from beluno.db.models.groups import Group, GroupInvite, GroupMembership
from beluno.db.models.iam import AuthSession, User
from beluno.db.models.plans import (
    Plan,
    PlanInvite,
    PlanParticipant,
    PlanSeries,
    TravelPlanDetails,
    TravelSegment,
)
from beluno.modules.context import CommandContext
from beluno.modules.groups.service import FALLBACK_DISPLAY_NAME, GroupView, MemberView
from beluno.modules.groups.service import LIVE_STATES as LIVE_MEMBERSHIPS
from beluno.modules.plans.service import PlanView
from beluno.modules.plans.travel import TravelView
from beluno.sync.pull import HIDDEN, Hidden, SnapshotRow
from beluno.sync.scopes import AccessLevel, ScopeKey

RowT = TypeVar("RowT", bound=Base)
Loaded = BaseModel | Hidden | None
Loader = Callable[[CommandContext, ScopeKey, AccessLevel, UUID], Awaitable[Loaded]]
Pager = Callable[
    [CommandContext, ScopeKey, AccessLevel, UUID | None, int], Awaitable[list[SnapshotRow]]
]

SNAPSHOT_ORDER: dict[str, tuple[str, ...]] = {
    "user": ("user", "session", "plan_series", "group_access", "plan_access"),
    "group": ("group", "group_membership", "group_invite", "plan_series"),
    "plan": ("plan", "plan_participant", "travel_details", "travel_segment", "plan_invite"),
}

VISIBLE_TYPES: dict[tuple[str, AccessLevel], frozenset[str]] = {
    ("user", AccessLevel.SELF): frozenset(SNAPSHOT_ORDER["user"]),
    ("group", AccessLevel.MANAGER): frozenset(SNAPSHOT_ORDER["group"]),
    ("group", AccessLevel.MEMBER): frozenset({"group", "group_membership", "plan_series"}),
    ("group", AccessLevel.INVITED): frozenset({"group", "group_membership"}),
    ("plan", AccessLevel.MANAGER): frozenset(SNAPSHOT_ORDER["plan"]),
    ("plan", AccessLevel.MEMBER): frozenset(
        {"plan", "plan_participant", "travel_details", "travel_segment"}
    ),
    ("plan", AccessLevel.READER): frozenset(
        {"plan", "plan_participant", "travel_details", "travel_segment"}
    ),
}


def session_response(session: AuthSession, current_session_id: UUID) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        auth_method=session.auth_method,
        platform=session.platform,
        device_label=session.device_label,
        app_version=session.app_version,
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        authenticated_at=session.authenticated_at,
        current=session.id == current_session_id,
    )


async def _page(
    ctx: CommandContext,
    statement: Select[RowT],
    key: InstrumentedAttribute[UUID],
    after: UUID | None,
    limit: int,
) -> list[RowT]:
    if after is not None:
        statement = statement.where(key > after)
    rows = await ctx.session.execute(statement.order_by(key).limit(limit))
    return list(rows.scalars())


async def _member(ctx: CommandContext, membership: GroupMembership) -> BaseModel:
    user = await ctx.session.get(User, membership.user_id)
    name = user.display_name if user else FALLBACK_DISPLAY_NAME
    return member_response(MemberView(membership=membership, display_name=name))


async def _plan_view(ctx: CommandContext, plan: Plan) -> BaseModel:
    # Entities never embed other entities: the caller's participant row is its own item.
    rendered = plan_response(PlanView(plan=plan, participant=None))
    return PlanEntity.model_validate(rendered.model_dump(exclude={"my_participant"}))


async def _travel_view(ctx: CommandContext, details: TravelPlanDetails) -> BaseModel:
    rendered = travel_response(TravelView(details=details, segments=[]))
    return TravelDetailsEntity.model_validate(rendered.model_dump(exclude={"segments"}))


def plan_access_signal(participant: PlanParticipant) -> PlanAccessSignal:
    return PlanAccessSignal.model_validate(
        {
            "plan_id": participant.plan_id,
            "participant_id": participant.id,
            "role": participant.role,
            "access_state": participant.access_state,
            "rsvp_status": participant.rsvp_status,
            "version": participant.version,
        }
    )


def group_access_signal(membership: GroupMembership) -> GroupAccessSignal:
    return GroupAccessSignal.model_validate(
        {
            "group_id": membership.group_id,
            "role": membership.role,
            "state": membership.state,
            "version": membership.version,
        }
    )


# --- single-entity loaders (change feed) -------------------------------------------


async def _load_plan_access(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    participant = await find_user_participant(ctx, id, ctx.require_actor().user_id)
    return plan_access_signal(participant) if participant else None


async def _load_group_access(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    membership = await ctx.session.get(GroupMembership, (id, ctx.require_actor().user_id))
    return group_access_signal(membership) if membership else None


async def _load_user(ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID) -> Loaded:
    user = await ctx.session.get(User, id)
    return profile_response(user) if user and user.id == ctx.require_actor().user_id else None


async def _load_session(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    actor = ctx.require_actor()
    auth_session = await ctx.session.get(AuthSession, id)
    if auth_session is None or auth_session.user_id != actor.user_id:
        return None
    if auth_session.revoked_at is not None or ctx.now >= auth_session.idle_expires_at:
        return None
    return session_response(auth_session, actor.session_id)


async def _load_series(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    series = await ctx.session.get(PlanSeries, id)
    return series_response(series) if series else None


async def _load_group(ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID) -> Loaded:
    group = await ctx.session.get(Group, id)
    if group is None:
        return None
    membership = await ctx.session.get(GroupMembership, (group.id, ctx.require_actor().user_id))
    return group_response(GroupView(group=group, membership=membership))


async def _load_membership(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    if level is AccessLevel.INVITED and id != ctx.require_actor().user_id:
        return HIDDEN
    membership = await ctx.session.get(GroupMembership, (scope.scope_id, id))
    if membership is None or membership.state not in LIVE_MEMBERSHIPS:
        return None
    return await _member(ctx, membership)


async def _load_group_invite(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    invite = await ctx.session.get(GroupInvite, id)
    return invite_response(invite) if invite and invite.group_id == scope.scope_id else None


async def _load_plan(ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID) -> Loaded:
    plan = await ctx.session.get(Plan, id)
    return await _plan_view(ctx, plan) if plan else None


async def _load_participant(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    participant = await ctx.session.get(PlanParticipant, id)
    if participant is None or participant.plan_id != scope.scope_id:
        return None
    pending = participant.access_state == AccessState.PENDING_APPROVAL.value
    if pending and level is not AccessLevel.MANAGER:
        return HIDDEN
    return participant_response(participant)


async def _load_travel_details(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    details = await ctx.session.get(TravelPlanDetails, id)
    if details is None or details.plan_id != scope.scope_id:
        return None
    return await _travel_view(ctx, details)


async def _load_segment(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    segment = await ctx.session.get(TravelSegment, id)
    if segment is None or segment.plan_id != scope.scope_id or segment.deleted_at is not None:
        return None
    return segment_response(segment)


async def _load_plan_invite(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    invite = await ctx.session.get(PlanInvite, id)
    return invite_response(invite) if invite and invite.plan_id == scope.scope_id else None


LOADERS: dict[str, Loader] = {
    "plan_access": _load_plan_access,
    "group_access": _load_group_access,
    "user": _load_user,
    "session": _load_session,
    "plan_series": _load_series,
    "group": _load_group,
    "group_membership": _load_membership,
    "group_invite": _load_group_invite,
    "plan": _load_plan,
    "plan_participant": _load_participant,
    "travel_details": _load_travel_details,
    "travel_segment": _load_segment,
    "plan_invite": _load_plan_invite,
}


# --- snapshot pagers ---------------------------------------------------------------


async def _page_plan_access(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(PlanParticipant).where(
        PlanParticipant.user_id == scope.scope_id,
        PlanParticipant.access_state != AccessState.MERGED.value,
    )
    rows = await _page(ctx, statement, PlanParticipant.plan_id, after, limit)
    return [SnapshotRow(row.plan_id, row.version, plan_access_signal(row)) for row in rows]


async def _page_group_access(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(GroupMembership).where(GroupMembership.user_id == scope.scope_id)
    rows = await _page(ctx, statement, GroupMembership.group_id, after, limit)
    return [SnapshotRow(row.group_id, row.version, group_access_signal(row)) for row in rows]


async def _page_user(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    user = await ctx.session.get(User, scope.scope_id)
    if user is None or after is not None:
        return []
    return [SnapshotRow(user.id, user.version, profile_response(user))]


async def _page_sessions(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(AuthSession).where(
        AuthSession.user_id == scope.scope_id,
        AuthSession.revoked_at.is_(None),
        AuthSession.idle_expires_at > ctx.now,
    )
    current = ctx.require_actor().session_id
    rows = await _page(ctx, statement, AuthSession.id, after, limit)
    return [SnapshotRow(row.id, 1, session_response(row, current)) for row in rows]


async def _page_series(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    if scope.scope_type.value == "user":
        statement = select(PlanSeries).where(
            PlanSeries.created_by_user_id == scope.scope_id, PlanSeries.group_id.is_(None)
        )
    else:
        statement = select(PlanSeries).where(PlanSeries.group_id == scope.scope_id)
    rows = await _page(ctx, statement, PlanSeries.id, after, limit)
    return [SnapshotRow(row.id, row.version, series_response(row)) for row in rows]


async def _page_group(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    group = await ctx.session.get(Group, scope.scope_id)
    if group is None or after is not None:
        return []
    membership = await ctx.session.get(GroupMembership, (group.id, ctx.require_actor().user_id))
    view = GroupView(group=group, membership=membership)
    return [SnapshotRow(group.id, group.version, group_response(view))]


async def _page_memberships(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(GroupMembership).where(
        GroupMembership.group_id == scope.scope_id,
        GroupMembership.state.in_(LIVE_MEMBERSHIPS),
    )
    if level is AccessLevel.INVITED:
        statement = statement.where(GroupMembership.user_id == ctx.require_actor().user_id)
    rows = await _page(ctx, statement, GroupMembership.user_id, after, limit)
    return [SnapshotRow(row.user_id, row.version, await _member(ctx, row)) for row in rows]


async def _page_group_invites(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(GroupInvite).where(GroupInvite.group_id == scope.scope_id)
    rows = await _page(ctx, statement, GroupInvite.id, after, limit)
    return [SnapshotRow(row.id, row.version, invite_response(row)) for row in rows]


async def _page_plan(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    plan = await ctx.session.get(Plan, scope.scope_id)
    if plan is None or after is not None:
        return []
    return [SnapshotRow(plan.id, plan.version, await _plan_view(ctx, plan))]


async def _page_participants(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(PlanParticipant).where(PlanParticipant.plan_id == scope.scope_id)
    if level is not AccessLevel.MANAGER:
        statement = statement.where(
            PlanParticipant.access_state != AccessState.PENDING_APPROVAL.value
        )
    rows = await _page(ctx, statement, PlanParticipant.id, after, limit)
    return [SnapshotRow(row.id, row.version, participant_response(row)) for row in rows]


async def _page_travel_details(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    details = await ctx.session.get(TravelPlanDetails, scope.scope_id)
    if details is None or after is not None:
        return []
    return [SnapshotRow(details.plan_id, details.version, await _travel_view(ctx, details))]


async def _page_segments(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(TravelSegment).where(
        TravelSegment.plan_id == scope.scope_id, TravelSegment.deleted_at.is_(None)
    )
    rows = await _page(ctx, statement, TravelSegment.id, after, limit)
    return [SnapshotRow(row.id, row.version, segment_response(row)) for row in rows]


async def _page_plan_invites(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(PlanInvite).where(PlanInvite.plan_id == scope.scope_id)
    rows = await _page(ctx, statement, PlanInvite.id, after, limit)
    return [SnapshotRow(row.id, row.version, invite_response(row)) for row in rows]


PAGERS: dict[str, Pager] = {
    "plan_access": _page_plan_access,
    "group_access": _page_group_access,
    "user": _page_user,
    "session": _page_sessions,
    "plan_series": _page_series,
    "group": _page_group,
    "group_membership": _page_memberships,
    "group_invite": _page_group_invites,
    "plan": _page_plan,
    "plan_participant": _page_participants,
    "travel_details": _page_travel_details,
    "travel_segment": _page_segments,
    "plan_invite": _page_plan_invites,
}


class FeedProjector:
    def snapshot_order(self, scope_type: str) -> tuple[str, ...]:
        return SNAPSHOT_ORDER[scope_type]

    def visible_types(self, scope_type: str, level: AccessLevel) -> frozenset[str]:
        return VISIBLE_TYPES.get((scope_type, level), frozenset())

    async def load(
        self,
        ctx: CommandContext,
        scope: ScopeKey,
        level: AccessLevel,
        entity_type: str,
        entity_id: UUID,
    ) -> Loaded:
        loader = LOADERS.get(entity_type)
        # Unknown types (introduced by a newer build) are skipped rather than deleted.
        return await loader(ctx, scope, level, entity_id) if loader else HIDDEN

    async def snapshot_page(
        self,
        ctx: CommandContext,
        scope: ScopeKey,
        level: AccessLevel,
        entity_type: str,
        after: UUID | None,
        limit: int,
    ) -> list[SnapshotRow]:
        pager = PAGERS.get(entity_type)
        return await pager(ctx, scope, level, after, limit) if pager else []
