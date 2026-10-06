"""Optional travel extension of a plan."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import travel as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import (
    IdempotencyKey,
    IfMatch,
    command_call,
    finish,
    finish_empty,
    set_etag,
)
from beluno.api.presenters import travel_response
from beluno.api.problems import problem_responses
from beluno.contracts.plans import (
    TravelDetailsRequest,
    TravelResponse,
    TravelSegmentRequest,
    TravelSegmentResponse,
)
from beluno.modules.context import open_context
from beluno.modules.plans import travel
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/plans/{plan_id}/travel", tags=["travel"])

READ_ERRORS = problem_responses(401, 404, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 503)


@router.get("", response_model=TravelResponse, responses=READ_ERRORS)
async def get_travel(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> TravelResponse:
    async with open_context(runtime, actor) as ctx:
        view = await travel.get_travel(ctx, plan_id)
    set_etag(response, view.details.version)
    return travel_response(view)


@router.put("", response_model=TravelResponse, responses=WRITE_ERRORS)
async def put_travel_details(
    plan_id: UUID,
    body: TravelDetailsRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> TravelResponse:
    """Create the travel extension, or replace it with the current ``If-Match`` version."""

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.TRAVEL_PUT_DETAILS, call, body))


@router.post(
    "/segments",
    status_code=status.HTTP_201_CREATED,
    response_model=TravelSegmentResponse,
    responses=WRITE_ERRORS,
)
async def add_segment(
    plan_id: UUID,
    body: TravelSegmentRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> TravelSegmentResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.TRAVEL_SEGMENT_ADD, call, body))


@router.put("/segments/{segment_id}", response_model=TravelSegmentResponse, responses=WRITE_ERRORS)
async def update_segment(
    plan_id: UUID,
    segment_id: UUID,
    body: TravelSegmentRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> TravelSegmentResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, segment_id=segment_id)
    return finish(response, await runner.run(actor, commands.TRAVEL_SEGMENT_UPDATE, call, body))


@router.delete(
    "/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS
)
async def delete_segment(
    plan_id: UUID,
    segment_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    call = command_call(idempotency_key, plan_id=plan_id, segment_id=segment_id)
    result = await runner.run(actor, commands.TRAVEL_SEGMENT_DELETE, call, EmptyPayload())
    return finish_empty(result)
