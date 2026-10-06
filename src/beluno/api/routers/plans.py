"""Plans, participants, RSVP, lifecycle, and ownership."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.http import (
    DEFAULT_PAGE_SIZE,
    CursorParam,
    IfMatch,
    LimitParam,
    decode_cursor,
    encode_cursor,
    parse_if_match,
    set_etag,
)
from beluno.api.problems import problem_responses
from beluno.authorization.policy import PlanRole, PlanState, Visibility
from beluno.contracts.common import Page
from beluno.contracts.plans import (
    JoinRequestDecision,
    ParticipantAddRequest,
    ParticipantResponse,
    ParticipantRoleRequest,
    ParticipantSeed,
    PlanCreateRequest,
    PlanDuplicateRequest,
    PlanOwnershipTransferRequest,
    PlanResponse,
    PlanStateChangeRequest,
    PlanTiming,
    PlanUpdateRequest,
    RsvpRequest,
)
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits
from beluno.modules.plans import duplication, service
from beluno.modules.plans import participants as participant_service
from beluno.modules.plans.participants import Seed

router = APIRouter(prefix="/v1/plans", tags=["plans"])

READ_ERRORS = problem_responses(401, 404, 422, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 503)


def participant_response(participant: PlanParticipant) -> ParticipantResponse:
    return ParticipantResponse.model_validate(
        {
            "id": participant.id,
            "plan_id": participant.plan_id,
            "identity_kind": participant.identity_kind,
            "user_id": participant.user_id,
            "display_name": participant.display_name,
            "role": participant.role,
            "access_state": participant.access_state,
            "rsvp_status": participant.rsvp_status,
            "rsvp_updated_at": participant.rsvp_updated_at,
            "merged_into_participant_id": participant.merged_into_participant_id,
            "joined_at": participant.joined_at,
            "version": participant.version,
        }
    )


def timing_contract(timing: service.Timing) -> PlanTiming:
    return PlanTiming.model_validate(timing.__dict__)


def timing_input(timing: PlanTiming) -> service.Timing:
    return service.Timing(
        mode=timing.mode,
        start_date=timing.start_date,
        end_date=timing.end_date,
        starts_at=timing.starts_at,
        ends_at=timing.ends_at,
        timezone=timing.timezone,
    )


def plan_response(view: service.PlanView) -> PlanResponse:
    plan = view.plan
    return PlanResponse.model_validate(
        {
            "id": plan.id,
            "group_id": plan.group_id,
            "series_id": plan.series_id,
            "occurrence_key": plan.occurrence_key,
            "is_series_exception": plan.is_series_exception,
            "title": plan.title,
            "kind": plan.kind,
            "state": plan.state,
            "timing": timing_contract(service.timing_of(plan)),
            "base_currency": plan.base_currency,
            "visibility": plan.visibility,
            "description": plan.description,
            "location_label": plan.location_label,
            "deletion_scheduled_at": plan.deletion_scheduled_at,
            "my_participant": participant_response(view.participant) if view.participant else None,
            "version": plan.version,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
        }
    )


def _with_etag(response: Response, view: service.PlanView) -> PlanResponse:
    set_etag(response, view.plan.version)
    return plan_response(view)


def seed_of(body: ParticipantSeed) -> Seed:
    return Seed(
        user_id=body.user_id,
        placeholder_name=body.placeholder_name,
        role=PlanRole(body.role),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
    responses=problem_responses(401, 403, 404, 409, 422, 503),
)
async def create_plan(
    body: PlanCreateRequest, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> PlanResponse:
    draft = service.PlanDraft(
        plan_id=body.id,
        group_id=body.group_id,
        title=body.title,
        kind=body.kind,
        state=PlanState(body.state),
        timing=timing_input(body.timing),
        base_currency=body.base_currency,
        visibility=Visibility(body.visibility) if body.visibility else None,
        description=body.description,
        location_label=body.location_label,
        seeds=tuple(seed_of(seed) for seed in body.participants),
        include_all_group_members=body.include_all_group_members,
    )
    async with open_context(runtime, actor) as ctx:
        view = await service.create_plan(ctx, draft)
    return _with_etag(response, view)


@router.get("", response_model=Page[PlanResponse], responses=READ_ERRORS)
async def list_plans(
    runtime: RuntimeDep,
    actor: ActorDep,
    group_id: UUID | None = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> Page[PlanResponse]:
    """Plans the caller participates in, or every plan they can see in one group."""

    async with open_context(runtime, actor) as ctx:
        views = await service.list_plans(
            ctx, group_id=group_id, after_id=decode_cursor(cursor), limit=limit
        )
    next_cursor = encode_cursor(views[-1].plan.id) if len(views) == limit else None
    return Page[PlanResponse](
        items=[plan_response(view) for view in views], next_cursor=next_cursor
    )


@router.get("/{plan_id}", response_model=PlanResponse, responses=READ_ERRORS)
async def get_plan(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> PlanResponse:
    async with open_context(runtime, actor) as ctx:
        view = await service.get_plan(ctx, plan_id)
    return _with_etag(response, view)


@router.patch("/{plan_id}", response_model=PlanResponse, responses=WRITE_ERRORS)
async def update_plan(
    plan_id: UUID,
    body: PlanUpdateRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> PlanResponse:
    expected_version = parse_if_match(if_match)
    fields = body.model_fields_set
    changes = service.PlanChanges(
        title=body.title,
        kind=body.kind,
        timing=timing_input(body.timing) if body.timing else None,
        base_currency=body.base_currency,
        visibility=Visibility(body.visibility) if body.visibility else None,
        description=body.description if "description" in fields else service.UNSET,
        location_label=body.location_label if "location_label" in fields else service.UNSET,
    )
    async with open_context(runtime, actor) as ctx:
        view = await service.update_plan(ctx, plan_id, expected_version, changes)
    return _with_etag(response, view)


@router.post("/{plan_id}/state", response_model=PlanResponse, responses=WRITE_ERRORS)
async def change_plan_state(
    plan_id: UUID,
    body: PlanStateChangeRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> PlanResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.change_state(ctx, plan_id, expected_version, PlanState(body.state))
    return _with_etag(response, view)


@router.delete("/{plan_id}", response_model=PlanResponse, responses=WRITE_ERRORS)
async def schedule_plan_deletion(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> PlanResponse:
    """Owner-only with a recent sign-in; restorable during the grace period."""

    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.schedule_deletion(ctx, plan_id, expected_version)
    return _with_etag(response, view)


@router.post("/{plan_id}/restore", response_model=PlanResponse, responses=WRITE_ERRORS)
async def restore_plan(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> PlanResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.restore_plan(ctx, plan_id, expected_version)
    return _with_etag(response, view)


@router.post("/{plan_id}/ownership-transfer", response_model=PlanResponse, responses=WRITE_ERRORS)
async def transfer_plan_ownership(
    plan_id: UUID,
    body: PlanOwnershipTransferRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> PlanResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        access = await participant_service.transfer_ownership(
            ctx, plan_id, body.new_owner_participant_id, expected_version
        )
    return _with_etag(response, service.PlanView(plan=access.plan, participant=access.participant))


@router.get(
    "/{plan_id}/participants", response_model=list[ParticipantResponse], responses=READ_ERRORS
)
async def list_participants(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    include_inactive: Annotated[bool, Query()] = False,
) -> list[ParticipantResponse]:
    async with open_context(runtime, actor) as ctx:
        rows = await participant_service.list_participants(
            ctx, plan_id, include_inactive=include_inactive
        )
    return [participant_response(row) for row in rows]


@router.post(
    "/{plan_id}/participants",
    status_code=status.HTTP_201_CREATED,
    response_model=ParticipantResponse,
    responses=WRITE_ERRORS,
)
async def add_participant(
    plan_id: UUID, body: ParticipantAddRequest, runtime: RuntimeDep, actor: ActorDep
) -> ParticipantResponse:
    async with open_context(runtime, actor) as ctx:
        participant = await participant_service.add_participant(ctx, plan_id, seed_of(body))
    return participant_response(participant)


@router.patch(
    "/{plan_id}/participants/{participant_id}",
    response_model=ParticipantResponse,
    responses=WRITE_ERRORS,
)
async def change_participant_role(
    plan_id: UUID,
    participant_id: UUID,
    body: ParticipantRoleRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    if_match: IfMatch = None,
) -> ParticipantResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        participant = await participant_service.change_role(
            ctx, plan_id, participant_id, PlanRole(body.role), expected_version
        )
    return participant_response(participant)


@router.delete(
    "/{plan_id}/participants/{participant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=WRITE_ERRORS,
)
async def remove_participant(
    plan_id: UUID, participant_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> Response:
    """Revoke access; the participant row and its history stay intact."""

    async with open_context(runtime, actor) as ctx:
        await participant_service.remove_participant(ctx, plan_id, participant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{plan_id}/participants/{participant_id}/review",
    response_model=ParticipantResponse,
    responses=WRITE_ERRORS,
)
async def review_join_request(
    plan_id: UUID,
    participant_id: UUID,
    body: JoinRequestDecision,
    runtime: RuntimeDep,
    actor: ActorDep,
) -> ParticipantResponse:
    async with open_context(runtime, actor) as ctx:
        participant = await participant_service.review_join_request(
            ctx, plan_id, participant_id, body.approve
        )
    return participant_response(participant)


@router.post("/{plan_id}/join", response_model=ParticipantResponse, responses=WRITE_ERRORS)
async def join_plan(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> ParticipantResponse:
    async with open_context(runtime, actor) as ctx:
        participant = await participant_service.join_plan(ctx, plan_id)
    return participant_response(participant)


@router.post("/{plan_id}/leave", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS)
async def leave_plan(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> Response:
    async with open_context(runtime, actor) as ctx:
        await participant_service.leave_plan(ctx, plan_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{plan_id}/rsvp", response_model=ParticipantResponse, responses=WRITE_ERRORS)
async def respond_rsvp(
    plan_id: UUID, body: RsvpRequest, runtime: RuntimeDep, actor: ActorDep
) -> ParticipantResponse:
    """Attendance answer only; it never changes the caller's access or role."""

    async with open_context(runtime, actor) as ctx:
        participant = await participant_service.respond_rsvp(ctx, plan_id, body.status)
    return participant_response(participant)


@router.post(
    "/{plan_id}/duplicate",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
    responses=problem_responses(401, 403, 404, 409, 422, 429, 503),
)
async def duplicate_plan(
    plan_id: UUID,
    body: PlanDuplicateRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
) -> PlanResponse:
    """Start a new plan from an allowlisted copy; financial and private history never copy."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.DUPLICATION_PER_USER, str(actor.user_id)
    )
    options = duplication.DuplicateOptions(
        title=body.title,
        use_source_group="group_id" not in body.model_fields_set,
        group_id=body.group_id,
        timing=timing_input(body.timing),
        participant_ids=tuple(body.participant_ids) if body.participant_ids is not None else None,
        include_description=body.include_description,
        include_location=body.include_location,
        include_travel_details=body.include_travel_details,
    )
    async with open_context(runtime, actor) as ctx:
        view = await duplication.duplicate_plan(ctx, plan_id, options)
    return _with_etag(response, view)
