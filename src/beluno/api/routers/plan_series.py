"""Recurring plan series."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.http import IfMatch, parse_if_match, set_etag
from beluno.api.problems import problem_responses
from beluno.api.routers.plans import plan_response
from beluno.authorization.policy import Visibility
from beluno.contracts.plans import (
    SeriesCreateRequest,
    SeriesResponse,
    SeriesSplitRequest,
    SeriesWithOccurrencesResponse,
)
from beluno.db.models.plans import PlanSeries
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits
from beluno.modules.plans import series as series_service
from beluno.modules.plans.service import PlanView

router = APIRouter(prefix="/v1/plan-series", tags=["plan series"])

WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 429, 503)


def series_response(series: PlanSeries) -> SeriesResponse:
    return SeriesResponse.model_validate(
        {
            "id": series.id,
            "group_id": series.group_id,
            "title": series.title,
            "kind": series.kind,
            "base_currency": series.base_currency,
            "visibility": series.visibility,
            "timezone": series.timezone,
            "start_date": series.start_date,
            "local_start_time": series.local_start_time,
            "duration_minutes": series.duration_minutes,
            "recurrence_rule": series.recurrence_rule,
            "horizon_days": series.horizon_days,
            "materialized_through": series.materialized_through,
            "state": series.state,
            "version": series.version,
            "created_at": series.created_at,
        }
    )


async def _with_occurrences(
    runtime: RuntimeDep, actor: ActorDep, series_id: UUID, response: Response
) -> SeriesWithOccurrencesResponse:
    async with open_context(runtime, actor) as ctx:
        series = await series_service.get_series(ctx, series_id)
        rows = await series_service.list_occurrences(ctx, series_id, from_date=None, limit=100)
    set_etag(response, series.version)
    return SeriesWithOccurrencesResponse(
        series=series_response(series),
        occurrences=[plan_response(PlanView(plan=plan, participant=mine)) for plan, mine in rows],
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SeriesWithOccurrencesResponse,
    responses=WRITE_ERRORS,
)
async def create_series(
    body: SeriesCreateRequest, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> SeriesWithOccurrencesResponse:
    """Creates the series and materializes occurrences inside its horizon."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.SERIES_CREATION_PER_USER, str(actor.user_id)
    )
    draft = series_service.SeriesDraft(
        group_id=body.group_id,
        title=body.title,
        kind=body.kind,
        base_currency=body.base_currency,
        visibility=Visibility(body.visibility) if body.visibility else None,
        description=body.description,
        location_label=body.location_label,
        timezone=body.timezone,
        start_date=body.start_date,
        local_start_time=body.local_start_time,
        duration_minutes=body.duration_minutes,
        recurrence_rule=body.recurrence_rule,
        participant_user_ids=tuple(body.participant_user_ids),
        horizon_days=body.horizon_days,
    )
    async with open_context(runtime, actor) as ctx:
        result = await series_service.create_series(ctx, draft)
    return await _with_occurrences(runtime, actor, result.series.id, response)


@router.get(
    "/{series_id}",
    response_model=SeriesWithOccurrencesResponse,
    responses=problem_responses(401, 404, 503),
)
async def get_series(
    series_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> SeriesWithOccurrencesResponse:
    return await _with_occurrences(runtime, actor, series_id, response)


@router.post(
    "/{series_id}/split", response_model=SeriesWithOccurrencesResponse, responses=WRITE_ERRORS
)
async def split_series(
    series_id: UUID,
    body: SeriesSplitRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> SeriesWithOccurrencesResponse:
    """Returns the successor series that carries the changed rule or fields."""

    expected_version = parse_if_match(if_match)
    changes = series_service.SeriesChanges(
        title=body.title,
        local_start_time=body.local_start_time,
        duration_minutes=body.duration_minutes,
        recurrence_rule=body.recurrence_rule,
        timezone=body.timezone,
    )
    async with open_context(runtime, actor) as ctx:
        result = await series_service.split_series(
            ctx, series_id, expected_version, from_date=body.from_date, changes=changes
        )
    return await _with_occurrences(runtime, actor, result.series.id, response)


@router.post("/{series_id}/cancel", response_model=SeriesResponse, responses=WRITE_ERRORS)
async def cancel_series(
    series_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> SeriesResponse:
    """Stops the series and cancels its untouched upcoming occurrences."""

    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        series = await series_service.cancel_series(ctx, series_id, expected_version)
    set_etag(response, series.version)
    return series_response(series)
