"""Recurring plan series commands."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from beluno.api.commands.groups import required_version
from beluno.api.presenters import plan_response, series_response
from beluno.authorization.policy import Visibility
from beluno.contracts.plans import (
    SeriesCreateRequest,
    SeriesResponse,
    SeriesSplitRequest,
    SeriesWithOccurrencesResponse,
)
from beluno.modules.context import CommandContext
from beluno.modules.iam import rate_limits
from beluno.modules.plans import series as series_service
from beluno.modules.plans.service import PlanView
from beluno.sync.commands import Command, CommandCall, EmptyPayload, version_of

OCCURRENCE_PAGE = 100


async def with_occurrences(ctx: CommandContext, series_id: UUID) -> SeriesWithOccurrencesResponse:
    series = await series_service.get_series(ctx, series_id)
    rows = await series_service.list_occurrences(
        ctx, series_id, from_date=None, limit=OCCURRENCE_PAGE
    )
    return SeriesWithOccurrencesResponse(
        series=series_response(series),
        occurrences=[plan_response(PlanView(plan=plan, participant=mine)) for plan, mine in rows],
    )


async def _create(
    ctx: CommandContext, call: CommandCall, body: SeriesCreateRequest
) -> SeriesWithOccurrencesResponse:
    draft = series_service.SeriesDraft(
        series_id=body.id,
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
    result = await series_service.create_series(ctx, draft)
    return await with_occurrences(ctx, result.series.id)


async def _split(
    ctx: CommandContext, call: CommandCall, body: SeriesSplitRequest
) -> SeriesWithOccurrencesResponse:
    changes = series_service.SeriesChanges(
        title=body.title,
        local_start_time=body.local_start_time,
        duration_minutes=body.duration_minutes,
        recurrence_rule=body.recurrence_rule,
        timezone=body.timezone,
    )
    result = await series_service.split_series(
        ctx, call.id("series_id"), required_version(call), from_date=body.from_date, changes=changes
    )
    return await with_occurrences(ctx, result.series.id)


async def _cancel(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> SeriesResponse:
    series = await series_service.cancel_series(ctx, call.id("series_id"), required_version(call))
    return series_response(series)


def _series_version(body: SeriesWithOccurrencesResponse) -> int:
    return body.series.version


SERIES_CREATE = Command(
    name="series.create",
    payload_model=SeriesCreateRequest,
    response_model=SeriesWithOccurrencesResponse,
    handler=_create,
    status=201,
    rate_limit=rate_limits.SERIES_CREATION_PER_USER,
    etag=_series_version,
)
SERIES_SPLIT = Command(
    name="series.split",
    payload_model=SeriesSplitRequest,
    response_model=SeriesWithOccurrencesResponse,
    handler=_split,
    target_fields=("series_id",),
    versioned=True,
    etag=_series_version,
)
SERIES_CANCEL = Command(
    name="series.cancel",
    payload_model=EmptyPayload,
    response_model=SeriesResponse,
    handler=_cancel,
    target_fields=("series_id",),
    versioned=True,
    etag=version_of,
)

COMMANDS: list[Command[Any, Any]] = [SERIES_CREATE, SERIES_SPLIT, SERIES_CANCEL]
