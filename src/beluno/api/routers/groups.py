"""Groups and memberships."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import groups as commands
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
from beluno.api.presenters import group_response, member_response
from beluno.api.problems import problem_responses
from beluno.contracts.common import Page
from beluno.contracts.groups import (
    GroupCreateRequest,
    GroupResponse,
    GroupUpdateRequest,
    InvitationAnswerRequest,
    MemberAddRequest,
    MemberResponse,
    MemberRoleChangeRequest,
    OwnershipTransferRequest,
)
from beluno.modules.context import open_context
from beluno.modules.groups import service
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/groups", tags=["groups"])

READ_ERRORS = problem_responses(401, 404, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 503)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=GroupResponse,
    responses=problem_responses(401, 403, 409, 422, 503),
)
async def create_group(
    body: GroupCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> GroupResponse:
    result = await runner.run(actor, commands.GROUP_CREATE, command_call(idempotency_key), body)
    return finish(response, result)


@router.get("", response_model=Page[GroupResponse], responses=READ_ERRORS)
async def list_groups(
    runtime: RuntimeDep,
    actor: ActorDep,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> Page[GroupResponse]:
    async with open_context(runtime, actor) as ctx:
        views = await service.list_groups(ctx, after_id=decode_cursor(cursor), limit=limit)
    next_cursor = encode_cursor(views[-1].group.id) if len(views) == limit else None
    return Page[GroupResponse](
        items=[group_response(view) for view in views], next_cursor=next_cursor
    )


@router.get("/{group_id}", response_model=GroupResponse, responses=READ_ERRORS)
async def get_group(
    group_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> GroupResponse:
    async with open_context(runtime, actor) as ctx:
        view = await service.get_group(ctx, group_id)
    set_etag(response, view.group.version)
    return group_response(view)


@router.patch("/{group_id}", response_model=GroupResponse, responses=WRITE_ERRORS)
async def update_group(
    group_id: UUID,
    body: GroupUpdateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> GroupResponse:
    call = command_call(idempotency_key, if_match=if_match, group_id=group_id)
    return finish(response, await runner.run(actor, commands.GROUP_UPDATE, call, body))


@router.delete("/{group_id}", response_model=GroupResponse, responses=WRITE_ERRORS)
async def schedule_group_deletion(
    group_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> GroupResponse:
    """Owner-only, requires a recent sign-in; the group can be restored during the grace period."""

    call = command_call(idempotency_key, if_match=if_match, group_id=group_id)
    result = await runner.run(actor, commands.GROUP_SCHEDULE_DELETION, call, EmptyPayload())
    return finish(response, result)


@router.post("/{group_id}/restore", response_model=GroupResponse, responses=WRITE_ERRORS)
async def restore_group(
    group_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> GroupResponse:
    call = command_call(idempotency_key, if_match=if_match, group_id=group_id)
    return finish(response, await runner.run(actor, commands.GROUP_RESTORE, call, EmptyPayload()))


@router.post("/{group_id}/ownership-transfer", response_model=GroupResponse, responses=WRITE_ERRORS)
async def transfer_group_ownership(
    group_id: UUID,
    body: OwnershipTransferRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> GroupResponse:
    call = command_call(idempotency_key, if_match=if_match, group_id=group_id)
    return finish(response, await runner.run(actor, commands.GROUP_TRANSFER_OWNERSHIP, call, body))


@router.get("/{group_id}/members", response_model=list[MemberResponse], responses=READ_ERRORS)
async def list_group_members(
    group_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> list[MemberResponse]:
    async with open_context(runtime, actor) as ctx:
        views = await service.list_members(ctx, group_id)
    return [member_response(view) for view in views]


@router.post(
    "/{group_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=MemberResponse,
    responses=problem_responses(401, 403, 404, 409, 422, 429, 503),
)
async def add_group_member(
    group_id: UUID,
    body: MemberAddRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> MemberResponse:
    call = command_call(idempotency_key, group_id=group_id)
    return finish(response, await runner.run(actor, commands.GROUP_MEMBER_ADD, call, body))


@router.post("/{group_id}/invitation", response_model=GroupResponse, responses=WRITE_ERRORS)
async def answer_group_invitation(
    group_id: UUID,
    body: InvitationAnswerRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> GroupResponse:
    call = command_call(idempotency_key, group_id=group_id)
    return finish(response, await runner.run(actor, commands.GROUP_INVITATION_ANSWER, call, body))


@router.patch(
    "/{group_id}/members/{user_id}", response_model=MemberResponse, responses=WRITE_ERRORS
)
async def change_group_member_role(
    group_id: UUID,
    user_id: UUID,
    body: MemberRoleChangeRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> MemberResponse:
    call = command_call(idempotency_key, if_match=if_match, group_id=group_id, user_id=user_id)
    return finish(response, await runner.run(actor, commands.GROUP_MEMBER_CHANGE_ROLE, call, body))


@router.delete(
    "/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 403, 404, 409, 422, 429, 503),
)
async def remove_group_member(
    group_id: UUID,
    user_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Remove a member; removing yourself leaves the group."""

    call = command_call(idempotency_key, group_id=group_id, user_id=user_id)
    return finish_empty(await runner.run(actor, commands.GROUP_MEMBER_REMOVE, call, EmptyPayload()))
