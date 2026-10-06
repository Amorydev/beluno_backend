"""Groups and memberships."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

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
from beluno.authorization.policy import GroupRole
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
from beluno.modules.iam import rate_limits

router = APIRouter(prefix="/v1/groups", tags=["groups"])

READ_ERRORS = problem_responses(401, 404, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 503)


def group_response(view: service.GroupView) -> GroupResponse:
    group, membership = view.group, view.membership
    return GroupResponse.model_validate(
        {
            "id": group.id,
            "name": group.name,
            "default_currency": group.default_currency,
            "default_timezone": group.default_timezone,
            "state": group.state,
            "deletion_scheduled_at": group.deletion_scheduled_at,
            "my_role": membership.role if membership else None,
            "my_membership_state": membership.state if membership else None,
            "version": group.version,
            "created_at": group.created_at,
            "updated_at": group.updated_at,
        }
    )


def member_response(view: service.MemberView) -> MemberResponse:
    membership = view.membership
    return MemberResponse.model_validate(
        {
            "user_id": membership.user_id,
            "display_name": view.display_name,
            "role": membership.role,
            "state": membership.state,
            "joined_at": membership.joined_at,
            "version": membership.version,
        }
    )


def _with_etag(response: Response, view: service.GroupView) -> GroupResponse:
    set_etag(response, view.group.version)
    return group_response(view)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=GroupResponse,
    responses=problem_responses(401, 403, 409, 422, 503),
)
async def create_group(
    body: GroupCreateRequest, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> GroupResponse:
    async with open_context(runtime, actor) as ctx:
        view = await service.create_group(
            ctx,
            group_id=body.id,
            name=body.name,
            default_currency=body.default_currency,
            default_timezone=body.default_timezone,
        )
    return _with_etag(response, view)


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
    return _with_etag(response, view)


@router.patch("/{group_id}", response_model=GroupResponse, responses=WRITE_ERRORS)
async def update_group(
    group_id: UUID,
    body: GroupUpdateRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> GroupResponse:
    expected_version = parse_if_match(if_match)
    changes = service.GroupChanges(
        name=body.name,
        default_currency=body.default_currency,
        default_timezone=body.default_timezone,
    )
    async with open_context(runtime, actor) as ctx:
        view = await service.update_group(ctx, group_id, expected_version, changes)
    return _with_etag(response, view)


@router.delete("/{group_id}", response_model=GroupResponse, responses=WRITE_ERRORS)
async def schedule_group_deletion(
    group_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> GroupResponse:
    """Owner-only, requires a recent sign-in; the group can be restored during the grace period."""

    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.schedule_deletion(ctx, group_id, expected_version)
    return _with_etag(response, view)


@router.post("/{group_id}/restore", response_model=GroupResponse, responses=WRITE_ERRORS)
async def restore_group(
    group_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> GroupResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.restore_group(ctx, group_id, expected_version)
    return _with_etag(response, view)


@router.post("/{group_id}/ownership-transfer", response_model=GroupResponse, responses=WRITE_ERRORS)
async def transfer_group_ownership(
    group_id: UUID,
    body: OwnershipTransferRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
) -> GroupResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.transfer_ownership(
            ctx, group_id, body.new_owner_user_id, expected_version
        )
    return _with_etag(response, view)


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
    group_id: UUID, body: MemberAddRequest, runtime: RuntimeDep, actor: ActorDep
) -> MemberResponse:
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.MEMBERSHIP_CHANGES_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        view = await service.add_member(ctx, group_id, body.user_id, GroupRole(body.role))
    return member_response(view)


@router.post("/{group_id}/invitation", response_model=GroupResponse, responses=WRITE_ERRORS)
async def answer_group_invitation(
    group_id: UUID,
    body: InvitationAnswerRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    response: Response,
) -> GroupResponse:
    async with open_context(runtime, actor) as ctx:
        view = await service.answer_invitation(ctx, group_id, body.accept)
    return _with_etag(response, view)


@router.patch(
    "/{group_id}/members/{user_id}", response_model=MemberResponse, responses=WRITE_ERRORS
)
async def change_group_member_role(
    group_id: UUID,
    user_id: UUID,
    body: MemberRoleChangeRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
    if_match: IfMatch = None,
) -> MemberResponse:
    expected_version = parse_if_match(if_match)
    async with open_context(runtime, actor) as ctx:
        view = await service.change_member_role(
            ctx, group_id, user_id, GroupRole(body.role), expected_version
        )
    return member_response(view)


@router.delete(
    "/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=WRITE_ERRORS,
)
async def remove_group_member(
    group_id: UUID, user_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> Response:
    """Remove a member; removing yourself leaves the group."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.MEMBERSHIP_CHANGES_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        await service.remove_member(ctx, group_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
