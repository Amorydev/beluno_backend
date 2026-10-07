"""Polls: the group decides by single choice, or yes/no with an optional quorum.

The electorate is the participants active when the poll opens; votes are visible
(who chose what) while it is open. It opens through the database
(``decisions.open_poll`` snapshots who may vote and seals the options) and closes
once, through the database (``decisions.close_poll`` for the creator or an
organiser, ``close_due_poll`` for the deadline job), so a job racing an organiser
yields one immutable result. A yes/no poll passes when yes outnumbers no and
reaches the quorum, if one was set.
Acting on the result (save the winning place, put it on the itinerary) is a
separate, idempotent command per poll and action; a tie is settled once, by the
option the first action picked.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import BelunoError, conflict, forbidden, not_found, validation_error
from beluno.db.ids import new_id
from beluno.db.models.decisions import (
    Poll,
    PollElector,
    PollOption,
    PollOutcome,
    PollResult,
    PollVote,
)
from beluno.db.models.schedule_places import Place
from beluno.modules.activity.events import ActivityItem, ActivityType, item
from beluno.modules.context import CommandContext, Runtime
from beluno.modules.planning import itinerary, places
from beluno.modules.planning.common import (
    own_participant_id,
    planning_access,
    record_planning_change,
    require_author_or_manager,
)

POLL_ENTITY = "poll"
SINGLE_CHOICE, YES_NO = "single_choice", "yes_no"
OPEN, CLOSED = "open", "closed"
OPEN_SQL = text("SELECT decisions.open_poll(:poll_id)")
CLOSE_SQL = text("SELECT decisions.close_poll(:poll_id, :event_id, :audit_id)")
CLOSE_DUE_SQL = text("SELECT decisions.close_due_poll(:event_id, :audit_id)")
MAX_CLOSES_PER_RUN = 500


@dataclass(frozen=True)
class OptionDraft:
    label: str
    place_id: UUID | None = None


@dataclass(frozen=True)
class PollDraft:
    kind: str
    question: str
    options: list[OptionDraft]
    deadline_at: datetime | None = None
    quorum: int | None = None
    allow_vote_change: bool = True


@dataclass(frozen=True)
class OutcomeRequest:
    action: str
    result_version: int = 1
    option_id: UUID | None = None
    day: date | None = None
    start_time: time | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class PollView:
    poll: Poll
    options: list[PollOption]
    votes: list[PollVote]
    eligible: int
    result: PollResult | None
    outcomes: list[PollOutcome] = field(default_factory=list)


def poll_closed() -> BelunoError:
    return conflict("POLL_CLOSED", "This poll is closed", "Its result is final")


async def list_polls(ctx: CommandContext, plan_id: UUID) -> list[PollView]:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    rows = await ctx.session.execute(
        select(Poll).where(Poll.plan_id == plan_id, Poll.deleted_at.is_(None)).order_by(Poll.id)
    )
    return await poll_views(ctx, list(rows.scalars()))


async def get_poll(ctx: CommandContext, plan_id: UUID, poll_id: UUID) -> PollView:
    await planning_access(ctx, plan_id, PlanAction.VIEW)
    return (await poll_views(ctx, [await _find(ctx, plan_id, poll_id)]))[0]


async def create_poll(
    ctx: CommandContext, plan_id: UUID, poll_id: UUID | None, draft: PollDraft
) -> PollView:
    await planning_access(ctx, plan_id, PlanAction.CONTRIBUTE_PLANNING)
    if draft.deadline_at is not None and draft.deadline_at <= ctx.now:
        raise validation_error("deadline_at must be in the future")
    for option in draft.options:
        if option.place_id is not None:
            await _require_place(ctx, plan_id, option.place_id)
    poll = Poll(
        id=poll_id or new_id(),
        plan_id=plan_id,
        kind=draft.kind,
        question=draft.question,
        deadline_at=draft.deadline_at,
        quorum=draft.quorum,
        allow_vote_change=draft.allow_vote_change,
        status=OPEN,
        created_by_user_id=ctx.require_actor().user_id,
        closed_at=None,
        closed_by_user_id=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(poll)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    drafts = draft.options or [OptionDraft("Yes"), OptionDraft("No")]
    for position, option in enumerate(drafts):
        ctx.session.add(
            PollOption(
                id=new_id(),
                poll_id=poll.id,
                plan_id=plan_id,
                label=option.label,
                place_id=option.place_id,
                answer=("yes", "no")[position] if draft.kind == YES_NO else None,
                position=position,
            )
        )
    await ctx.session.flush()
    # The database snapshots who may vote (placeholders excluded) and seals the options.
    eligible = (await ctx.session.execute(OPEN_SQL, {"poll_id": poll.id})).scalar_one()
    if draft.quorum is not None and draft.quorum > eligible:
        raise validation_error(f"quorum cannot exceed the {eligible} people who may vote")
    await _record(
        ctx, poll, "planning.poll_created", item(ActivityType.POLL_CREATED, poll_kind=poll.kind)
    )
    return (await poll_views(ctx, [poll]))[0]


async def vote(ctx: CommandContext, plan_id: UUID, poll_id: UUID, option_id: UUID) -> PollView:
    """Cast or change the caller's vote; the same vote again changes nothing."""

    access = await planning_access(ctx, plan_id, PlanAction.RESPOND_PLANNING)
    poll = await _find(ctx, plan_id, poll_id, for_update=True)
    _require_open(ctx, poll)
    option = await ctx.session.scalar(
        select(PollOption).where(PollOption.poll_id == poll.id, PollOption.id == option_id)
    )
    if option is None:
        raise validation_error("option_id is not an option of this poll")
    voter = own_participant_id(access)
    if await ctx.session.get(PollElector, (poll.id, voter)) is None:
        raise forbidden("Only people in the trip when the poll opened vote on it")
    ballot = await ctx.session.get(PollVote, (poll.id, voter))
    if ballot is not None and ballot.option_id == option_id:
        return (await poll_views(ctx, [poll]))[0]
    if ballot is None:
        ctx.session.add(
            PollVote(
                poll_id=poll.id,
                participant_id=voter,
                plan_id=plan_id,
                option_id=option_id,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
        )
    elif not poll.allow_vote_change:
        raise conflict("VOTE_LOCKED", "Votes on this poll cannot be changed")
    else:
        ballot.option_id = option_id
        ballot.updated_at = ctx.now
    await _bump(ctx, poll, "planning.poll_voted")
    return (await poll_views(ctx, [poll]))[0]


async def close_poll(ctx: CommandContext, plan_id: UUID, poll_id: UUID) -> PollView:
    """Close early (the creator or an organiser); closing a closed poll returns its result."""

    access = await planning_access(ctx, plan_id, PlanAction.VIEW)
    poll = await _find(ctx, plan_id, poll_id)
    await require_author_or_manager(ctx, access, poll.created_by_user_id)
    if poll.status == OPEN:
        await ctx.session.execute(
            CLOSE_SQL, {"poll_id": poll.id, "event_id": new_id(), "audit_id": new_id()}
        )
    return (await poll_views(ctx, [await _find(ctx, plan_id, poll_id, refresh=True)]))[0]


async def delete_poll(ctx: CommandContext, plan_id: UUID, poll_id: UUID) -> None:
    access = await planning_access(ctx, plan_id, PlanAction.VIEW)
    poll = await _find(ctx, plan_id, poll_id, for_update=True)
    await require_author_or_manager(ctx, access, poll.created_by_user_id)
    # A closed poll, or one past its deadline, is kept with its result.
    _require_open(ctx, poll)
    poll.deleted_at = ctx.now
    await _bump(ctx, poll, "planning.poll_deleted", operation="delete")


async def apply_outcome(
    ctx: CommandContext, plan_id: UUID, poll_id: UUID, request: OutcomeRequest
) -> PollView:
    """Save the winning place, or put the winner on the itinerary (once per action)."""

    access = await planning_access(ctx, plan_id, PlanAction.VIEW, for_update=True)
    poll = await _find(ctx, plan_id, poll_id, for_update=True)
    await require_author_or_manager(ctx, access, poll.created_by_user_id)
    result = await ctx.session.get(PollResult, poll.id)
    if poll.kind != SINGLE_CHOICE or result is None:
        raise conflict(
            "POLL_NOT_DECIDED", "Only a closed single-choice poll has a winner to act on"
        )
    if request.result_version != result.version:
        raise conflict("POLL_RESULT_CHANGED", "The result you acted on is not the current one")
    earlier = {
        outcome.action: outcome
        for outcome in (
            await ctx.session.execute(select(PollOutcome).where(PollOutcome.poll_id == poll.id))
        ).scalars()
    }
    # A tie is settled once: every action of the poll uses the option picked first.
    settled = next(iter(earlier.values())).option_id if earlier else None
    chosen = _chosen_option(result, request.option_id or settled)
    if settled is not None and chosen != settled:
        raise conflict(
            "OUTCOME_ALREADY_APPLIED", "This result was already acted on with another option"
        )
    if request.action in earlier:
        return (await poll_views(ctx, [poll]))[0]
    option = await ctx.session.scalar(
        select(PollOption).where(PollOption.poll_id == poll.id, PollOption.id == chosen)
    )
    assert option is not None
    saved_place = earlier.get("save_place")
    planned_item = earlier.get("add_to_plan")
    if request.action == "save_place":
        created = await _save_place(ctx, plan_id, option)
        if planned_item is not None and option.place_id is None:
            # The winner was planned before it was saved: the item now points at it.
            await itinerary.attach_place(ctx, plan_id, planned_item.created_entity_id, created)
    else:
        place_id = option.place_id or (saved_place.created_entity_id if saved_place else None)
        view = await itinerary.create_item(
            ctx,
            plan_id,
            None,
            itinerary.ItemDraft(
                title=option.label,
                day=request.day,
                start_time=request.start_time,
                timezone=request.timezone,
                place_id=place_id,
            ),
        )
        created = view.item.id
    ctx.session.add(
        PollOutcome(
            poll_id=poll.id,
            action=request.action,
            plan_id=plan_id,
            result_version=result.version,
            option_id=chosen,
            created_entity_id=created,
            created_by_user_id=ctx.require_actor().user_id,
            created_at=ctx.now,
        )
    )
    await _bump(ctx, poll, "planning.poll_outcome_applied")
    return (await poll_views(ctx, [poll]))[0]


async def close_due_polls(runtime: Runtime) -> int:
    """Worker: close every poll past its deadline, one committed poll at a time."""

    closed = 0
    for _ in range(MAX_CLOSES_PER_RUN):
        async with runtime.database.transaction() as session:
            poll_id = (
                await session.execute(CLOSE_DUE_SQL, {"event_id": new_id(), "audit_id": new_id()})
            ).scalar_one()
        if poll_id is None:
            break
        closed += 1
    return closed


async def poll_views(ctx: CommandContext, polls: list[Poll]) -> list[PollView]:
    if not polls:
        return []
    ids = [poll.id for poll in polls]
    options = _by_poll(
        (
            await ctx.session.execute(
                select(PollOption).where(PollOption.poll_id.in_(ids)).order_by(PollOption.position)
            )
        ).scalars()
    )
    votes = _by_poll(
        (
            await ctx.session.execute(
                select(PollVote).where(PollVote.poll_id.in_(ids)).order_by(PollVote.participant_id)
            )
        ).scalars()
    )
    electors = _by_poll(
        (
            await ctx.session.execute(select(PollElector).where(PollElector.poll_id.in_(ids)))
        ).scalars()
    )
    outcomes = _by_poll(
        (
            await ctx.session.execute(
                select(PollOutcome).where(PollOutcome.poll_id.in_(ids)).order_by(PollOutcome.action)
            )
        ).scalars()
    )
    results = {
        result.poll_id: result
        for result in (
            await ctx.session.execute(select(PollResult).where(PollResult.poll_id.in_(ids)))
        ).scalars()
    }
    return [
        PollView(
            poll=poll,
            options=options.get(poll.id, []),
            votes=votes.get(poll.id, []),
            eligible=len(electors.get(poll.id, [])),
            result=results.get(poll.id),
            outcomes=outcomes.get(poll.id, []),
        )
        for poll in polls
    ]


class _PollRow(Protocol):
    poll_id: UUID


RowT = TypeVar("RowT", bound=_PollRow)


def _by_poll(rows: Iterable[RowT]) -> dict[UUID, list[RowT]]:
    grouped: dict[UUID, list[RowT]] = {}
    for row in rows:
        grouped.setdefault(row.poll_id, []).append(row)
    return grouped


def _chosen_option(result: PollResult, requested: UUID | None) -> UUID:
    if result.outcome == "winner":
        assert result.winner_option_id is not None
        if requested not in (None, result.winner_option_id):
            raise validation_error("option_id is not the winning option")
        return result.winner_option_id
    if result.outcome == "tie":
        if requested is None or requested not in result.tied_option_ids:
            raise validation_error("The poll is tied: name one of the tied options in option_id")
        return requested
    raise conflict("POLL_NOT_DECIDED", "Nobody voted, so there is no winner to act on")


async def _save_place(ctx: CommandContext, plan_id: UUID, option: PollOption) -> UUID:
    if option.place_id is not None:
        return (await places.mark_poll_winner(ctx, plan_id, option.place_id)).place.id
    draft = places.PlaceDraft(name=option.label, maps_url=None, category="other", note=None)
    view = await places.create_place(ctx, plan_id, None, draft, status=places.POLL_WINNER)
    return view.place.id


def _require_open(ctx: CommandContext, poll: Poll) -> None:
    if poll.status != OPEN or (poll.deadline_at is not None and poll.deadline_at <= ctx.now):
        raise poll_closed()


async def _require_place(ctx: CommandContext, plan_id: UUID, place_id: UUID) -> None:
    found = await ctx.session.scalar(
        select(Place.id).where(
            Place.plan_id == plan_id, Place.id == place_id, Place.deleted_at.is_(None)
        )
    )
    if found is None:
        raise validation_error("place_id does not name a saved place of this plan")


async def _find(
    ctx: CommandContext,
    plan_id: UUID,
    poll_id: UUID,
    *,
    for_update: bool = False,
    refresh: bool = False,
) -> Poll:
    statement = select(Poll).where(Poll.plan_id == plan_id, Poll.id == poll_id)
    if for_update:
        statement = statement.with_for_update()
    if for_update or refresh:
        statement = statement.execution_options(populate_existing=True)
    poll = (await ctx.session.execute(statement)).scalar_one_or_none()
    if poll is None or poll.deleted_at is not None:
        raise not_found()
    return poll


async def _bump(
    ctx: CommandContext,
    poll: Poll,
    action: str,
    *,
    operation: str = "upsert",
    activity: ActivityItem | None = None,
) -> None:
    poll.version += 1
    poll.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, poll, action, activity, operation=operation)


async def _record(
    ctx: CommandContext,
    poll: Poll,
    action: str,
    activity: ActivityItem | None = None,
    *,
    operation: str = "upsert",
) -> None:
    await record_planning_change(
        ctx,
        action=action,
        entity_type=POLL_ENTITY,
        entity_id=poll.id,
        entity_version=poll.version,
        plan_id=poll.plan_id,
        metadata={"version": poll.version, "status": poll.status},
        operation=operation,
        activity=activity,
    )
