"""Crews: the caller's private, saved lists of people they plan with."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import crews as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import (
    IdempotencyKey,
    IfMatch,
    command_call,
    finish,
    finish_empty,
    set_etag,
)
from beluno.api.presenters import crew_response
from beluno.api.problems import problem_responses
from beluno.contracts.people import CrewCreateRequest, CrewResponse, CrewUpdateRequest
from beluno.modules.context import open_context
from beluno.modules.people import crews
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/crews", tags=["people"])

READ_ERRORS = problem_responses(401, 404, 503)
WRITE_ERRORS = problem_responses(401, 404, 409, 412, 422, 428, 429, 503)


@router.get("", response_model=list[CrewResponse], responses=READ_ERRORS)
async def list_crews(runtime: RuntimeDep, actor: ActorDep) -> list[CrewResponse]:
    async with open_context(runtime, actor) as ctx:
        views = await crews.list_crews(ctx)
    return [crew_response(view) for view in views]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=CrewResponse, responses=WRITE_ERRORS
)
async def create_crew(
    body: CrewCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> CrewResponse:
    """Save people you plan with; ``from_plan_id`` saves everyone active in that plan."""

    call = command_call(idempotency_key)
    return finish(response, await runner.run(actor, commands.CREW_CREATE, call, body))


@router.get("/{crew_id}", response_model=CrewResponse, responses=READ_ERRORS)
async def get_crew(
    crew_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> CrewResponse:
    async with open_context(runtime, actor) as ctx:
        view = await crews.get_crew(ctx, crew_id)
    set_etag(response, view.crew.version)
    return crew_response(view)


@router.patch("/{crew_id}", response_model=CrewResponse, responses=WRITE_ERRORS)
async def update_crew(
    crew_id: UUID,
    body: CrewUpdateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> CrewResponse:
    call = command_call(idempotency_key, if_match=if_match, crew_id=crew_id)
    return finish(response, await runner.run(actor, commands.CREW_UPDATE, call, body))


@router.delete("/{crew_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS)
async def delete_crew(
    crew_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    call = command_call(idempotency_key, crew_id=crew_id)
    return finish_empty(await runner.run(actor, commands.CREW_DELETE, call, EmptyPayload()))
