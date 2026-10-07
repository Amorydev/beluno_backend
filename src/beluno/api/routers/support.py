"""Report a problem (S18): the record check shown before sending, and the report."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from beluno.api.dependencies import ActorDep, RuntimeDep
from beluno.api.problems import problem_responses
from beluno.contracts.errors import validation_error
from beluno.contracts.support import (
    LinkedEntityType,
    ProblemReportRequest,
    ProblemReportResponse,
    RecordCheckResponse,
)
from beluno.modules import support
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits

router = APIRouter(prefix="/v1/support", tags=["support"])


@router.get(
    "/checks", response_model=RecordCheckResponse, responses=problem_responses(401, 404, 422, 503)
)
async def check_record(
    runtime: RuntimeDep,
    actor: ActorDep,
    plan_id: UUID,
    entity_type: LinkedEntityType | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
) -> RecordCheckResponse:
    """Whether the plan's ledger reconciles and the record belongs to the plan."""

    if (entity_type is None) != (entity_id is None):
        raise validation_error("entity_type and entity_id go together")
    linked = (
        support.Linked(entity_type, entity_id)
        if entity_type is not None and entity_id is not None
        else None
    )
    async with open_context(runtime, actor) as ctx:
        result = await support.check_record(ctx, plan_id, linked)
    return RecordCheckResponse(
        plan_id=result.plan_id,
        ledger_ok=result.ledger_ok,
        record_found=result.record_found,
        record_version=result.record_version,
    )


@router.post(
    "/reports",
    status_code=status.HTTP_201_CREATED,
    response_model=ProblemReportResponse,
    responses=problem_responses(401, 404, 422, 429, 503),
)
async def report_problem(
    body: ProblemReportRequest, runtime: RuntimeDep, actor: ActorDep
) -> ProblemReportResponse:
    """Send a report; with diagnostics attached, its code is returned to quote to support."""

    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.PROBLEM_REPORTS_PER_USER, str(actor.user_id)
    )
    draft = support.ReportDraft(
        category=body.category,
        message=body.message,
        plan_id=body.plan_id,
        linked=support.Linked(body.linked.entity_type, body.linked.entity_id)
        if body.linked
        else None,
        attach_diagnostics=body.attach_diagnostics,
        client=body.client.model_dump(mode="json", exclude_none=True) if body.client else None,
    )
    async with open_context(runtime, actor) as ctx:
        report = await support.report_problem(ctx, draft)
    return ProblemReportResponse(
        id=report.id, diagnostic_code=report.diagnostic_code, created_at=report.created_at
    )
