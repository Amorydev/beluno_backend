"""The caller's profile and device sessions."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import profile as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import (
    IdempotencyKey,
    IfMatch,
    command_call,
    finish,
    finish_empty,
    set_etag,
)
from beluno.api.presenters import profile_response
from beluno.api.problems import problem_responses
from beluno.contracts.iam import ProfileUpdateRequest, SessionResponse, UserProfileResponse
from beluno.modules.context import open_context
from beluno.modules.iam import users
from beluno.modules.iam.sessions import list_live_sessions
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/me", tags=["me"])


@router.get("", response_model=UserProfileResponse, responses=problem_responses(401, 503))
async def get_profile(
    runtime: RuntimeDep, actor: ActorDep, response: Response
) -> UserProfileResponse:
    async with open_context(runtime, actor) as ctx:
        user = await users.load_user(ctx, ctx.require_actor().user_id)
    set_etag(response, user.version)
    return profile_response(user)


@router.patch(
    "",
    response_model=UserProfileResponse,
    responses=problem_responses(401, 409, 412, 422, 428, 503),
)
async def update_profile(
    body: ProfileUpdateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> UserProfileResponse:
    call = command_call(idempotency_key, if_match=if_match)
    return finish(response, await runner.run(actor, commands.PROFILE_UPDATE, call, body))


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 403, 409, 503),
)
async def delete_account(
    runner: RunnerDep, actor: ActorDep, idempotency_key: IdempotencyKey = None
) -> Response:
    """Delete your account (needs a recent sign-in, except for guests).

    You become "Former member" in every plan and money history stays intact.
    Refused with 409 OWNER_TRANSFER_REQUIRED while you own a plan another person
    (not a placeholder) is still in; plans you own alone are scheduled for
    deletion. Success signs out every session, so a retry after a lost response
    answers 401: treat it as done.
    """

    call = command_call(idempotency_key)
    return finish_empty(await runner.run(actor, commands.PROFILE_DELETE, call, EmptyPayload()))


@router.get(
    "/sessions", response_model=list[SessionResponse], responses=problem_responses(401, 503)
)
async def list_sessions(runtime: RuntimeDep, actor: ActorDep) -> list[SessionResponse]:
    async with open_context(runtime, actor) as ctx:
        current = ctx.require_actor()
        sessions = await list_live_sessions(ctx, current.user_id)
    return [
        SessionResponse(
            id=item.id,
            auth_method=item.auth_method,
            platform=item.platform,
            device_label=item.device_label,
            app_version=item.app_version,
            created_at=item.created_at,
            last_seen_at=item.last_seen_at,
            authenticated_at=item.authenticated_at,
            current=item.id == current.session_id,
        )
        for item in sessions
    ]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_responses(401, 404, 503),
)
async def revoke_device_session(
    session_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Sign a device out remotely; its refresh and access tokens stop working immediately."""

    call = command_call(idempotency_key, session_id=session_id)
    return finish_empty(await runner.run(actor, commands.SESSION_REVOKE, call, EmptyPayload()))
