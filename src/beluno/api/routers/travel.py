"""Optional travel extension of a plan."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.http import IfMatch, parse_if_match, set_etag
from beluno.api.problems import problem_responses
from beluno.contracts.plans import (
    TravelDetailsRequest,
    TravelResponse,
    TravelSegmentRequest,
    TravelSegmentResponse,
)
from beluno.db.models.plans import TravelSegment
from beluno.modules.context import open_context
from beluno.modules.plans import travel

router = APIRouter(prefix="/v1/plans/{plan_id}/travel", tags=["travel"])

READ_ERRORS = problem_responses(401, 404, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 503)


def segment_response(segment: TravelSegment) -> TravelSegmentResponse:
    return TravelSegmentResponse.model_validate(
        {
            "id": segment.id,
            "segment_type": segment.segment_type,
            "title": segment.title,
            "origin_label": segment.origin_label,
            "destination_label": segment.destination_label,
            "timing_mode": segment.timing_mode,
            "start_date": segment.start_date,
            "end_date": segment.end_date,
            "departure_local": segment.departure_local,
            "departure_timezone": segment.departure_timezone,
            "departs_at": segment.departs_at,
            "arrival_local": segment.arrival_local,
            "arrival_timezone": segment.arrival_timezone,
            "arrives_at": segment.arrives_at,
            "sort_order": segment.sort_order,
            "version": segment.version,
        }
    )


def travel_response(view: travel.TravelView) -> TravelResponse:
    return TravelResponse(
        plan_id=view.details.plan_id,
        destination_summary=view.details.destination_summary,
        notes=view.details.notes,
        version=view.details.version,
        segments=[segment_response(segment) for segment in view.segments],
    )


def segment_input(body: TravelSegmentRequest) -> travel.SegmentInput:
    return travel.SegmentInput(
        segment_type=body.segment_type,
        title=body.title,
        origin_label=body.origin_label,
        destination_label=body.destination_label,
        timing_mode=body.timing_mode,
        start_date=body.start_date,
        end_date=body.end_date,
        departure_local=body.departure_local,
        departure_timezone=body.departure_timezone,
        arrival_local=body.arrival_local,
        arrival_timezone=body.arrival_timezone,
        sort_order=body.sort_order,
    )


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
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> TravelResponse:
    """Create the travel extension, or replace it with the current ``If-Match`` version."""

    expected_version = parse_if_match(if_match) if if_match is not None else None
    async with open_context(runtime, actor) as ctx:
        view = await travel.put_details(
            ctx,
            plan_id,
            destination_summary=body.destination_summary,
            notes=body.notes,
            expected_version=expected_version,
        )
    set_etag(response, view.details.version)
    return travel_response(view)


@router.post(
    "/segments",
    status_code=status.HTTP_201_CREATED,
    response_model=TravelSegmentResponse,
    responses=WRITE_ERRORS,
)
async def add_segment(
    plan_id: UUID, body: TravelSegmentRequest, runtime: RuntimeDep, actor: ActorDep
) -> TravelSegmentResponse:
    async with open_context(runtime, actor) as ctx:
        segment = await travel.add_segment(ctx, plan_id, segment_input(body))
    return segment_response(segment)


@router.put("/segments/{segment_id}", response_model=TravelSegmentResponse, responses=WRITE_ERRORS)
async def update_segment(
    plan_id: UUID,
    segment_id: UUID,
    body: TravelSegmentRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    if_match: IfMatch = None,
) -> TravelSegmentResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        segment = await travel.update_segment(
            ctx, plan_id, segment_id, segment_input(body), expected_version
        )
    return segment_response(segment)


@router.delete(
    "/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS
)
async def delete_segment(
    plan_id: UUID, segment_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> Response:
    async with open_context(runtime, actor) as ctx:
        await travel.delete_segment(ctx, plan_id, segment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
