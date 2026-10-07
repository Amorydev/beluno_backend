"""Entity projections for the sync feed, rendered with the public presenters.

Visibility mirrors the REST resources: a scope's level decides which entity
types a caller receives at all, and row-level rules (pending participants and
invites for managers only) hide rows without revealing them. Rows that stopped
being visible come back as deletes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, select
from sqlalchemy.orm import InstrumentedAttribute

from beluno.api import finance_projection, planning_projection
from beluno.api.finance_projection import FINANCE_TYPES
from beluno.api.planning_projection import PLANNING_TYPES
from beluno.api.presenters import (
    crew_response,
    invite_response,
    participant_response,
    plan_response,
    profile_response,
)
from beluno.authorization.access import find_user_participant
from beluno.authorization.policy import AccessState
from beluno.contracts.activity import ActivityEventResponse
from beluno.contracts.iam import SessionResponse
from beluno.contracts.sync import PlanAccessSignal, PlanEntity
from beluno.db.models.activity import ActivityEvent
from beluno.db.models.base import Base
from beluno.db.models.iam import AuthSession, User
from beluno.db.models.people import Crew
from beluno.db.models.plans import Plan, PlanInvite, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.people.crews import crew_views
from beluno.modules.plans.service import PlanView
from beluno.sync.pull import HIDDEN, Hidden, SnapshotRow
from beluno.sync.scopes import AccessLevel, ScopeKey

RowT = TypeVar("RowT", bound=Base)
Loaded = BaseModel | Hidden | None
Loader = Callable[[CommandContext, ScopeKey, AccessLevel, UUID], Awaitable[Loaded]]
Pager = Callable[
    [CommandContext, ScopeKey, AccessLevel, UUID | None, int], Awaitable[list[SnapshotRow]]
]

SNAPSHOT_ORDER: dict[str, tuple[str, ...]] = {
    "user": ("user", "session", "plan_access", "crew", "packing_item", "activity_event"),
    "plan": (
        "plan",
        "plan_participant",
        "plan_invite",
        *FINANCE_TYPES,
        *PLANNING_TYPES,
        "activity_event",
    ),
}

VISIBLE_TYPES: dict[tuple[str, AccessLevel], frozenset[str]] = {
    ("user", AccessLevel.SELF): frozenset(SNAPSHOT_ORDER["user"]),
    ("plan", AccessLevel.MANAGER): frozenset(SNAPSHOT_ORDER["plan"]),
    ("plan", AccessLevel.MEMBER): frozenset(
        {"plan", "plan_participant", *FINANCE_TYPES, *PLANNING_TYPES, "activity_event"}
    ),
}


def activity_response(event: ActivityEvent) -> ActivityEventResponse:
    return ActivityEventResponse(
        id=event.id,
        type=event.type,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        plan_id=event.plan_id,
        actor_user_id=event.actor_user_id,
        summary=event.summary,
        occurred_at=event.occurred_at,
    )


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


async def _plan_view(ctx: CommandContext, plan: Plan) -> BaseModel:
    # Entities never embed other entities: the caller's participant row is its own item.
    rendered = plan_response(PlanView(plan=plan, participant=None))
    return PlanEntity.model_validate(rendered.model_dump(exclude={"my_participant"}))


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


# --- single-entity loaders (change feed) -------------------------------------------


async def _load_plan_access(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    participant = await find_user_participant(ctx, id, ctx.require_actor().user_id)
    return plan_access_signal(participant) if participant else None


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


async def _load_crew(ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID) -> Loaded:
    crew = await ctx.session.get(Crew, id)
    if crew is None or crew.owner_user_id != scope.scope_id or crew.deleted_at is not None:
        return None
    return crew_response((await crew_views(ctx, [crew]))[0])


async def _load_activity(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    event = await ctx.session.get(ActivityEvent, id)
    if event is None or (event.scope_type, event.scope_id) != (
        scope.scope_type.value,
        scope.scope_id,
    ):
        return None
    return activity_response(event)


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


async def _load_plan_invite(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> Loaded:
    invite = await ctx.session.get(PlanInvite, id)
    return invite_response(invite) if invite and invite.plan_id == scope.scope_id else None


LOADERS: dict[str, Loader] = {
    "plan_access": _load_plan_access,
    "user": _load_user,
    "session": _load_session,
    "crew": _load_crew,
    "activity_event": _load_activity,
    "plan": _load_plan,
    "plan_participant": _load_participant,
    "plan_invite": _load_plan_invite,
    "ledger": finance_projection.load_ledger,
    "expense": finance_projection.load_expense,
    "settlement": finance_projection.load_settlement,
    "budget": finance_projection.load_budget,
    "cost_commitment": finance_projection.load_commitment,
    "fund": finance_projection.load_fund,
    "fund_movement": finance_projection.load_fund_movement,
    "fund_count": finance_projection.load_fund_count,
    "consolidation": finance_projection.load_consolidation,
    "place": planning_projection.load_place,
    "itinerary_item": planning_projection.load_item,
    "poll": planning_projection.load_poll,
    "booking": planning_projection.load_booking,
    "task": planning_projection.load_task,
    "packing_item": planning_projection.load_packing,
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


async def _page_crews(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Crew).where(Crew.owner_user_id == scope.scope_id, Crew.deleted_at.is_(None))
    views = await crew_views(ctx, await _page(ctx, statement, Crew.id, after, limit))
    return [SnapshotRow(view.crew.id, view.crew.version, crew_response(view)) for view in views]


async def _page_activity(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(ActivityEvent).where(
        ActivityEvent.scope_type == scope.scope_type.value,
        ActivityEvent.scope_id == scope.scope_id,
    )
    rows = await _page(ctx, statement, ActivityEvent.id, after, limit)
    return [SnapshotRow(row.id, 1, activity_response(row)) for row in rows]


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


async def _page_plan_invites(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(PlanInvite).where(PlanInvite.plan_id == scope.scope_id)
    rows = await _page(ctx, statement, PlanInvite.id, after, limit)
    return [SnapshotRow(row.id, row.version, invite_response(row)) for row in rows]


PAGERS: dict[str, Pager] = {
    "plan_access": _page_plan_access,
    "user": _page_user,
    "session": _page_sessions,
    "crew": _page_crews,
    "activity_event": _page_activity,
    "plan": _page_plan,
    "plan_participant": _page_participants,
    "plan_invite": _page_plan_invites,
    "ledger": finance_projection.page_ledger,
    "expense": finance_projection.page_expenses,
    "settlement": finance_projection.page_settlements,
    "budget": finance_projection.page_budgets,
    "cost_commitment": finance_projection.page_commitments,
    "fund": finance_projection.page_fund,
    "fund_movement": finance_projection.page_fund_movements,
    "fund_count": finance_projection.page_fund_counts,
    "consolidation": finance_projection.page_consolidations,
    "place": planning_projection.page_places,
    "itinerary_item": planning_projection.page_items,
    "poll": planning_projection.page_polls,
    "booking": planning_projection.page_bookings,
    "task": planning_projection.page_tasks,
    "packing_item": planning_projection.page_packing,
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
