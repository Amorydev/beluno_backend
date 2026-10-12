"""Downloads of a plan's data (CSV or JSON) and of the caller's whole account (JSON)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import anyio
from fastapi import APIRouter, Query, Response

from beluno.api import exports, paid_exports
from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.problems import problem_responses
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits

router = APIRouter(tags=["exports"])


def _file(*media_types: str) -> dict[str, object]:
    return {
        "description": "The file, as an attachment (Content-Disposition), never cached",
        "content": {media_type: {} for media_type in media_types},
    }


PLAN_FILE: dict[int | str, dict[str, object]] = {
    200: _file("text/csv", "application/json", "application/pdf"),
    **problem_responses(401, 403, 404, 409, 422, 429, 503),
}
ACCOUNT_FILE: dict[int | str, dict[str, object]] = {
    200: _file("application/json"),
    **problem_responses(401, 429, 503),
}


@router.get("/v1/plans/{plan_id}/export", response_class=Response, responses=PLAN_FILE)
async def export_plan(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    format: Literal["csv", "json", "accounting", "pdf"] = Query(default="csv"),
    lang: Literal["vi", "en"] | None = Query(
        default=None, description="The PDF's language; the caller's profile locale by default"
    ),
) -> Response:
    """Everyone in the plan may export what they can already see: CSV has one row per
    expense (original amount and base-currency snapshot); JSON has every entity sync
    gives them. Secrets and other people's private packing items are never included.

    With a Trip Pass (or the owner's Pro; hangouts are free): ``accounting`` is a CSV
    with one row per person and journal entry whose ``amount`` adds up to the balances;
    ``pdf`` is the trip report (trips only). Otherwise ``403 UPGRADE_REQUIRED``."""

    await rate_limits.enforce_rate_limit(runtime, rate_limits.EXPORTS_PER_USER, str(actor.user_id))
    async with open_context(runtime, actor) as ctx:
        if format == "csv":
            file = await exports.plan_csv(ctx, plan_id)
        elif format == "json":
            file = await exports.plan_json(ctx, plan_id)
        elif format == "accounting":
            file = await paid_exports.plan_accounting_csv(ctx, plan_id)
        else:
            file = await paid_exports.plan_pdf(ctx, plan_id, lang)
    return await _download(file)


@router.get("/v1/me/export", response_class=Response, responses=ACCOUNT_FILE)
async def export_account(runtime: RuntimeDep, actor: ActorDep) -> Response:
    """The caller's profile, devices, crews, own packing lists, and every plan they are
    still in, as JSON."""

    await rate_limits.enforce_rate_limit(runtime, rate_limits.EXPORTS_PER_USER, str(actor.user_id))
    async with open_context(runtime, actor) as ctx:
        file = await exports.account_json(ctx)
    return await _download(file)


async def _download(file: exports.ExportFile) -> Response:
    """Render once the transaction has closed, in a worker thread."""

    return Response(
        content=await anyio.to_thread.run_sync(file.render, limiter=file.limiter),
        media_type=file.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file.filename}"',
            "Cache-Control": "no-store",
        },
    )
