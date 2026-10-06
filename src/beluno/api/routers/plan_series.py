"""Recurring plan series."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import series as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import IdempotencyKey, IfMatch, command_call, finish, set_etag
from beluno.api.problems import problem_responses
from beluno.contracts.plans import (
    SeriesCreateRequest,
    SeriesResponse,
    SeriesSplitRequest,
    SeriesWithOccurrencesResponse,
)
from beluno.modules.context import open_context
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/plan-series", tags=["plan series"])

WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 429, 503)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SeriesWithOccurrencesResponse,
    responses=WRITE_ERRORS,
)
async def create_series(
    body: SeriesCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SeriesWithOccurrencesResponse:
    """Creates the series and materializes occurrences inside its horizon."""

    result = await runner.run(actor, commands.SERIES_CREATE, command_call(idempotency_key), body)
    return finish(response, result)


@router.get(
    "/{series_id}",
    response_model=SeriesWithOccurrencesResponse,
    responses=problem_responses(401, 404, 503),
)
async def get_series(
    series_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> SeriesWithOccurrencesResponse:
    async with open_context(runtime, actor) as ctx:
        view = await commands.with_occurrences(ctx, series_id)
    set_etag(response, view.series.version)
    return view


@router.post(
    "/{series_id}/split", response_model=SeriesWithOccurrencesResponse, responses=WRITE_ERRORS
)
async def split_series(
    series_id: UUID,
    body: SeriesSplitRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> SeriesWithOccurrencesResponse:
    """Returns the successor series that carries the changed rule or fields."""

    call = command_call(idempotency_key, if_match=if_match, series_id=series_id)
    return finish(response, await runner.run(actor, commands.SERIES_SPLIT, call, body))


@router.post("/{series_id}/cancel", response_model=SeriesResponse, responses=WRITE_ERRORS)
async def cancel_series(
    series_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> SeriesResponse:
    """Stops the series and cancels its untouched upcoming occurrences."""

    call = command_call(idempotency_key, if_match=if_match, series_id=series_id)
    return finish(response, await runner.run(actor, commands.SERIES_CANCEL, call, EmptyPayload()))
