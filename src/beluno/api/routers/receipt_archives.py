"""Every receipt of a plan in one zip, built in the background."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, status

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.problems import problem_responses
from beluno.contracts.media import ReceiptArchiveResponse
from beluno.modules import receipt_archives
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits

router = APIRouter(tags=["media"])
ArchiveState = Literal["pending", "building", "ready", "failed", "expired"]
ArchiveFailure = Literal["too_large", "storage"] | None


@router.post(
    "/v1/plans/{plan_id}/receipt-archives",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReceiptArchiveResponse,
    responses=problem_responses(401, 403, 404, 409, 429, 503),
)
async def request_receipt_archive(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> ReceiptArchiveResponse:
    """Ask for the plan's receipts in one zip (a Trip Pass or the owner's Pro; hangouts
    are free). Poll the archive until ``ready``; one still being built is reused."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.RECEIPT_ARCHIVES_PER_USER, str(actor.user_id)
    )
    async with open_context(runtime, actor) as ctx:
        found = await receipt_archives.request_archive(ctx, plan_id)
    return _response(found)


@router.get(
    "/v1/plans/{plan_id}/receipt-archives/{archive_id}",
    response_model=ReceiptArchiveResponse,
    responses=problem_responses(401, 404, 503),
)
async def get_receipt_archive(
    plan_id: UUID, archive_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> ReceiptArchiveResponse:
    """Only the person who asked sees the archive; ``download_url`` while it is ready."""

    async with open_context(runtime, actor) as ctx:
        found = await receipt_archives.get_archive(ctx, plan_id, archive_id)
    return _response(found)


def _response(found: receipt_archives.ArchiveView) -> ReceiptArchiveResponse:
    archive = found.archive
    return ReceiptArchiveResponse(
        id=archive.id,
        plan_id=archive.plan_id,
        state=cast(ArchiveState, found.state),
        failure=cast(ArchiveFailure, archive.failure),
        receipts=archive.receipts,
        size_bytes=archive.size_bytes,
        created_at=archive.created_at,
        ready_at=archive.ready_at,
        expires_at=archive.expires_at,
        download_url=found.download_url,
    )
