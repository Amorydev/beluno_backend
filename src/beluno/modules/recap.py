"""A plan's recap, computed from the ledger and the trip plan.

Money comes from the budget overview (paid expenses net of refunds, converted to
the base currency with the same rates budgets use) and "settled" from the ledger's
own status and rule, so the recap never disagrees with the budget or ledger
screens. Anyone who may see the plan's money sees its recap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select

from beluno.authorization.policy import AccessState
from beluno.db.models.decisions import Poll, PollResult
from beluno.db.models.finance import AccountBalance, LedgerAccount, LedgerHead, Settlement
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.db.models.schedule_places import ItineraryItem, Place, PlaceReaction
from beluno.modules.context import CommandContext
from beluno.modules.finance.budgets import get_budgets
from beluno.modules.finance.states import BudgetTier, LedgerStatus, SettlementStatus
from beluno.modules.planning import itinerary, polls

BASIS_POINTS = 10_000
# Results that decided something (a tie is settled by an organiser).
DECIDED = ("winner", "tie", "passed", "failed")


@dataclass(frozen=True)
class Stop:
    name: str
    code: str | None
    nights: int | None


@dataclass(frozen=True)
class TopPlace:
    place_id: UUID
    name: str
    wanted_by: int
    of_people: int


@dataclass(frozen=True)
class CrewMember:
    participant_id: UUID
    active: bool
    settled: bool


@dataclass(frozen=True)
class Recap:
    plan: Plan
    start: date | None
    end: date | None
    days: int | None
    stops: list[Stop]
    people: int
    currency: str
    spent_minor: int
    per_person_per_day_minor: int | None
    unconverted: bool
    estimated_rates: bool
    categories: list[tuple[str, int, int]]  # category, spent, basis points
    top_place: TopPlace | None
    itinerary_done: int
    itinerary_total: int
    polls_decided: int
    crew: list[CrewMember]
    all_settled: bool
    settled_on: date | None


async def get_recap(ctx: CommandContext, plan_id: UUID) -> Recap:
    overview = await get_budgets(ctx, plan_id)  # loads the plan and checks VIEW_FINANCE
    plan = await ctx.session.get(Plan, plan_id)
    assert plan is not None
    rows = list(
        (
            await ctx.session.execute(
                select(PlanParticipant)
                .where(
                    PlanParticipant.plan_id == plan_id,
                    PlanParticipant.access_state != AccessState.MERGED.value,
                )
                .order_by(PlanParticipant.id)
            )
        ).scalars()
    )
    active = [row for row in rows if row.access_state == AccessState.ACTIVE.value]
    start, end = _local_dates(plan)
    days = (end - start).days + 1 if start is not None and end is not None else None
    spent = overview.total.actual
    head = await ctx.session.get(LedgerHead, plan_id)
    all_settled = head is not None and head.status == LedgerStatus.SETTLED.value
    return Recap(
        plan=plan,
        start=start,
        end=end,
        days=days,
        stops=[_stop(stop) for stop in plan.destinations],
        people=len(active),
        currency=overview.currency,
        spent_minor=spent,
        per_person_per_day_minor=_rounded(spent, len(active) * days) if days and active else None,
        unconverted=any(tier is BudgetTier.ACTUAL for tier, _ in overview.unconverted),
        estimated_rates=overview.estimated_rates,
        categories=_categories({c: s.actual for c, s in overview.categories.items()}, spent),
        top_place=await _top_place(ctx, plan_id, sum(1 for r in active if r.user_id is not None)),
        itinerary_done=await _count(
            ctx,
            ItineraryItem,
            ItineraryItem.plan_id == plan_id,
            ItineraryItem.deleted_at.is_(None),
            ItineraryItem.status == itinerary.DONE,
        ),
        itinerary_total=await _count(
            ctx,
            ItineraryItem,
            ItineraryItem.plan_id == plan_id,
            ItineraryItem.deleted_at.is_(None),
            ItineraryItem.status != itinerary.CANCELLED,
        ),
        polls_decided=await _polls_decided(ctx, plan_id),
        crew=await _crew(ctx, plan, rows, head),
        all_settled=all_settled,
        settled_on=await _settled_on(ctx, plan_id) if all_settled else None,
    )


def _local_dates(plan: Plan) -> tuple[date | None, date | None]:
    """The plan's first and last calendar day: its dates, or its local start and end."""

    if plan.starts_at is None:
        return plan.start_date, plan.end_date or plan.start_date
    zone = ZoneInfo(plan.timezone) if plan.timezone else UTC
    start = plan.starts_at.astimezone(zone)
    end = (plan.ends_at or plan.starts_at).astimezone(zone)
    # Ending at local midnight ends on the day before.
    if end > start and end.time() == end.time().min:
        end -= timedelta(microseconds=1)
    return start.date(), end.date()


def _rounded(numerator: int, denominator: int) -> int:
    """Rounded half up; zero when there is nothing to divide by."""

    return (2 * numerator + denominator) // (2 * denominator) if denominator else 0


def _categories(spent_by: dict[str, int], total: int) -> list[tuple[str, int, int]]:
    """Largest spend first; shares in basis points that add up to exactly 10,000."""

    rows = sorted(((c, s) for c, s in spent_by.items() if s > 0), key=lambda r: (-r[1], r[0]))
    if not rows or total <= 0:
        return []
    floors = [spent * BASIS_POINTS // total for _, spent in rows]
    # Largest remainders take the points that flooring left over.
    order = sorted(range(len(rows)), key=lambda i: (-(rows[i][1] * BASIS_POINTS % total), i))
    for i in order[: BASIS_POINTS - sum(floors)]:
        floors[i] += 1
    return [(category, spent, share) for (category, spent), share in zip(rows, floors, strict=True)]


def _stop(stop: dict[str, Any]) -> Stop:
    start, end = stop.get("start_date"), stop.get("end_date")
    nights = (date.fromisoformat(end) - date.fromisoformat(start)).days if start and end else None
    return Stop(name=str(stop.get("name", "")), code=stop.get("code"), nights=nights)


async def _count(ctx: CommandContext, model: type[Any], *where: ColumnElement[bool]) -> int:
    found = await ctx.session.scalar(select(func.count()).select_from(model).where(*where))
    return int(found or 0)


async def _polls_decided(ctx: CommandContext, plan_id: UUID) -> int:
    found = await ctx.session.scalar(
        select(func.count())
        .select_from(Poll)
        .join(PollResult, PollResult.poll_id == Poll.id)
        .where(
            Poll.plan_id == plan_id,
            Poll.deleted_at.is_(None),
            Poll.status == polls.CLOSED,
            PollResult.outcome.in_(DECIDED),
        )
    )
    return int(found or 0)


async def _top_place(ctx: CommandContext, plan_id: UUID, people: int) -> TopPlace | None:
    """The place most active participants want to go to (as the places screen counts)."""

    wanted = func.count(PlaceReaction.participant_id)
    row = (
        await ctx.session.execute(
            select(Place.id, Place.name, wanted)
            .join(PlaceReaction, PlaceReaction.place_id == Place.id)
            .join(
                PlanParticipant,
                (PlanParticipant.plan_id == PlaceReaction.plan_id)
                & (PlanParticipant.id == PlaceReaction.participant_id),
            )
            .where(
                Place.plan_id == plan_id,
                Place.deleted_at.is_(None),
                PlaceReaction.wants.is_(True),
                PlanParticipant.access_state == AccessState.ACTIVE.value,
            )
            .group_by(Place.id, Place.name)
            .order_by(wanted.desc(), Place.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return TopPlace(place_id=row[0], name=row[1], wanted_by=int(row[2]), of_people=people)


async def _crew(
    ctx: CommandContext, plan: Plan, rows: list[PlanParticipant], head: LedgerHead | None
) -> list[CrewMember]:
    """Active participants, and anyone who left with money still open.

    A participant is square by the ledger's rule: every balance at zero, except
    that base-currency balances within the settle tolerance count as zero.
    """

    tolerance = head.settle_tolerance_minor if head is not None else 0
    balances = await ctx.session.execute(
        select(LedgerAccount.participant_id, LedgerAccount.currency, AccountBalance.balance_minor)
        .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
        .where(LedgerAccount.plan_id == plan.id, LedgerAccount.participant_id.is_not(None))
    )
    owing = {
        participant_id
        for participant_id, currency, balance in balances.all()
        if abs(balance) > (tolerance if currency == plan.base_currency else 0)
    }
    return [
        CrewMember(row.id, row.access_state == AccessState.ACTIVE.value, row.id not in owing)
        for row in rows
        if row.access_state == AccessState.ACTIVE.value or row.id in owing
    ]


async def _settled_on(ctx: CommandContext, plan_id: UUID) -> date | None:
    """The date of the last settlement still standing (as entered by people)."""

    return await ctx.session.scalar(
        select(func.max(Settlement.occurred_on)).where(
            Settlement.plan_id == plan_id,
            Settlement.status != SettlementStatus.REVERSED.value,
        )
    )
