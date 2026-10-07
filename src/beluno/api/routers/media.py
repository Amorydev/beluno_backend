"""A plan's files (receipts, trip covers): record, upload, download, delete.

Bytes go straight between the app and storage through presigned URLs; the API
records files, signs URLs, and queues the scan.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import media as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.http import IdempotencyKey, command_call, finish, finish_empty
from beluno.api.media_projection import media_response
from beluno.api.problems import problem_responses
from beluno.contracts.media import MediaCreateRequest, MediaResponse, SignedUrlResponse
from beluno.modules import media
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/plans/{plan_id}/media", tags=["media"])

READ_ERRORS = problem_responses(401, 404, 409, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 422, 429, 503)


@router.get("", response_model=list[MediaResponse], responses=READ_ERRORS)
async def list_media(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> list[MediaResponse]:
    async with open_context(runtime, actor) as ctx:
        rows = await media.media_of_plan(ctx, plan_id)
    return [media_response(row) for row in rows]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=MediaResponse, responses=WRITE_ERRORS
)
async def create_media(
    plan_id: UUID,
    body: MediaCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> MediaResponse:
    """Record a receipt (anyone who adds expenses) or a trip cover (organisers)."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.MEDIA_CREATE, call, body))


@router.post("/{media_id}/upload-url", response_model=SignedUrlResponse, responses=WRITE_ERRORS)
async def media_upload_url(
    plan_id: UUID, media_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> SignedUrlResponse:
    """Where to PUT the file (its uploader, while it awaits upload)."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.MEDIA_URLS_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        signed = await media.upload_url(ctx, plan_id, media_id)
    return SignedUrlResponse(url=signed.url, expires_at=signed.expires_at)


@router.post("/{media_id}/uploaded", response_model=MediaResponse, responses=WRITE_ERRORS)
async def media_uploaded(
    plan_id: UUID, media_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> MediaResponse:
    """The upload finished: the file is scanned and cleaned before anyone sees it."""

    async with open_context(runtime, actor) as ctx:
        scanned = await media.mark_uploaded(ctx, plan_id, media_id)
    return media_response(scanned)


@router.post("/{media_id}/download-url", response_model=SignedUrlResponse, responses=WRITE_ERRORS)
async def media_download_url(
    plan_id: UUID, media_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> SignedUrlResponse:
    """A short-lived link to a ready file, for anyone in the plan."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.MEDIA_URLS_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        signed = await media.download_url(ctx, plan_id, media_id)
    return SignedUrlResponse(url=signed.url, expires_at=signed.expires_at)


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS)
async def delete_media(
    plan_id: UUID,
    media_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Delete a file (its uploader or an organiser); storage forgets it soon after."""

    call = command_call(idempotency_key, plan_id=plan_id, media_id=media_id)
    return finish_empty(await runner.run(actor, commands.MEDIA_DELETE, call, EmptyPayload()))
