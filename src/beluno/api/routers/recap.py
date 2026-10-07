"""A plan's recap (S58) and the public-safe fields of its share card (S32)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.problems import problem_responses
from beluno.contracts.finance import ExpenseCategory
from beluno.contracts.recap import (
    RecapCategory,
    RecapPerson,
    RecapResponse,
    RecapShareCard,
    RecapStop,
    RecapTopPlace,
)
from beluno.modules import recap
from beluno.modules.context import open_context

router = APIRouter(prefix="/v1/plans/{plan_id}", tags=["recap"])


@router.get("/recap", response_model=RecapResponse, responses=problem_responses(401, 404, 422, 503))
async def get_recap(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> RecapResponse:
    """Spending, the plan, decisions, and who has settled; for anyone who sees the money.
    ``share`` holds the only fields a public card may show."""

    async with open_context(runtime, actor) as ctx:
        result = await recap.get_recap(ctx, plan_id)
    return recap_response(result)


def recap_response(result: recap.Recap) -> RecapResponse:
    plan = result.plan
    top = result.top_place
    return RecapResponse(
        plan_id=plan.id,
        title=plan.title,
        start_date=result.start,
        end_date=result.end,
        days=result.days,
        stops=[RecapStop(name=s.name, code=s.code, nights=s.nights) for s in result.stops],
        people=result.people,
        currency=result.currency,
        spent_minor=result.spent_minor,
        per_person_per_day_minor=result.per_person_per_day_minor,
        unconverted=result.unconverted,
        estimated_rates=result.estimated_rates,
        categories=[
            RecapCategory(
                category=cast(ExpenseCategory, category),
                spent_minor=spent,
                share_basis_points=share,
            )
            for category, spent, share in result.categories
        ],
        top_place=RecapTopPlace(
            place_id=top.place_id, name=top.name, wanted_by=top.wanted_by, of_people=top.of_people
        )
        if top
        else None,
        itinerary_done=result.itinerary_done,
        itinerary_total=result.itinerary_total,
        polls_decided=result.polls_decided,
        crew=[
            RecapPerson(participant_id=m.participant_id, active=m.active, settled=m.settled)
            for m in result.crew
        ],
        all_settled=result.all_settled,
        settled_on=result.settled_on,
        cover_media_id=plan.cover_media_id,
        highlights=result.highlights,
        memories=result.memories,
        share=RecapShareCard(
            route=[stop.name for stop in result.stops],
            start_date=result.start,
            days=result.days,
            people=result.people,
            cover_media_id=plan.cover_media_id,
            currency=result.currency,
            spent_minor=result.spent_minor,
            spent_complete=not result.unconverted,
        ),
    )
