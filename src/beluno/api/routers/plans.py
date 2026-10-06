"""Plans, participants, RSVP, lifecycle, and ownership."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from beluno.api.commands import plans as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import (
    DEFAULT_PAGE_SIZE,
    CursorParam,
    IdempotencyKey,
    IfMatch,
    LimitParam,
    command_call,
    decode_cursor,
    encode_cursor,
    finish,
    finish_empty,
    set_etag,
)
from beluno.api.presenters import participant_response, plan_response
from beluno.api.problems import problem_responses
from beluno.contracts.common import Page
from beluno.contracts.plans import (
    JoinRequestDecision,
    ParticipantAddRequest,
    ParticipantResponse,
    ParticipantRoleRequest,
    PlanCreateRequest,
    PlanDuplicateRequest,
    PlanOwnershipTransferRequest,
    PlanResponse,
    PlanStateChangeRequest,
    PlanUpdateRequest,
    RsvpRequest,
)
from beluno.modules.context import open_context
from beluno.modules.plans import participants as participant_service
from beluno.modules.plans import service
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/plans", tags=["plans"])

READ_ERRORS = problem_responses(401, 404, 422, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 503)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
    responses=problem_responses(401, 403, 404, 409, 422, 503),
)
async def create_plan(
    body: PlanCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    result = await runner.run(actor, commands.PLAN_CREATE, command_call(idempotency_key), body)
    return finish(response, result)


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
    set_etag(response, view.plan.version)
    return plan_response(view)


@router.patch("/{plan_id}", response_model=PlanResponse, responses=WRITE_ERRORS)
async def update_plan(
    plan_id: UUID,
    body: PlanUpdateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_UPDATE, call, body))


@router.post("/{plan_id}/state", response_model=PlanResponse, responses=WRITE_ERRORS)
async def change_plan_state(
    plan_id: UUID,
    body: PlanStateChangeRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_CHANGE_STATE, call, body))


@router.delete("/{plan_id}", response_model=PlanResponse, responses=WRITE_ERRORS)
async def schedule_plan_deletion(
    plan_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    """Owner-only with a recent sign-in; restorable during the grace period."""

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    result = await runner.run(actor, commands.PLAN_SCHEDULE_DELETION, call, EmptyPayload())
    return finish(response, result)


@router.post("/{plan_id}/restore", response_model=PlanResponse, responses=WRITE_ERRORS)
async def restore_plan(
    plan_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_RESTORE, call, EmptyPayload()))


@router.post("/{plan_id}/ownership-transfer", response_model=PlanResponse, responses=WRITE_ERRORS)
async def transfer_plan_ownership(
    plan_id: UUID,
    body: PlanOwnershipTransferRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_TRANSFER_OWNERSHIP, call, body))


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
    plan_id: UUID,
    body: ParticipantAddRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ParticipantResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_PARTICIPANT_ADD, call, body))


@router.patch(
    "/{plan_id}/participants/{participant_id}",
    response_model=ParticipantResponse,
    responses=WRITE_ERRORS,
)
async def change_participant_role(
    plan_id: UUID,
    participant_id: UUID,
    body: ParticipantRoleRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> ParticipantResponse:
    call = command_call(
        idempotency_key, if_match=if_match, plan_id=plan_id, participant_id=participant_id
    )
    result = await runner.run(actor, commands.PLAN_PARTICIPANT_CHANGE_ROLE, call, body)
    return finish(response, result)


@router.delete(
    "/{plan_id}/participants/{participant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=WRITE_ERRORS,
)
async def remove_participant(
    plan_id: UUID,
    participant_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Revoke access; the participant row and its history stay intact."""

    call = command_call(idempotency_key, plan_id=plan_id, participant_id=participant_id)
    result = await runner.run(actor, commands.PLAN_PARTICIPANT_REMOVE, call, EmptyPayload())
    return finish_empty(result)


@router.post(
    "/{plan_id}/participants/{participant_id}/review",
    response_model=ParticipantResponse,
    responses=WRITE_ERRORS,
)
async def review_join_request(
    plan_id: UUID,
    participant_id: UUID,
    body: JoinRequestDecision,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ParticipantResponse:
    call = command_call(idempotency_key, plan_id=plan_id, participant_id=participant_id)
    return finish(response, await runner.run(actor, commands.PLAN_PARTICIPANT_REVIEW, call, body))


@router.post("/{plan_id}/join", response_model=ParticipantResponse, responses=WRITE_ERRORS)
async def join_plan(
    plan_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ParticipantResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_JOIN, call, EmptyPayload()))


@router.post("/{plan_id}/leave", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS)
async def leave_plan(
    plan_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish_empty(await runner.run(actor, commands.PLAN_LEAVE, call, EmptyPayload()))


@router.put("/{plan_id}/rsvp", response_model=ParticipantResponse, responses=WRITE_ERRORS)
async def respond_rsvp(
    plan_id: UUID,
    body: RsvpRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ParticipantResponse:
    """Attendance answer only; it never changes the caller's access or role."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_RSVP, call, body))


@router.post(
    "/{plan_id}/duplicate",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
    responses=problem_responses(401, 403, 404, 409, 422, 429, 503),
)
async def duplicate_plan(
    plan_id: UUID,
    body: PlanDuplicateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    """Start a new plan from an allowlisted copy; financial and private history never copy."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_DUPLICATE, call, body))
