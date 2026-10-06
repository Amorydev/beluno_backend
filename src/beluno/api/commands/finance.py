"""Finance commands: expenses, refunds, settlements, waivers, budgets, and commitments."""

from __future__ import annotations

from typing import Any

from beluno.api.commands.groups import required_version
from beluno.api.finance_presenters import (
    budget_response,
    commitment_draft,
    commitment_response,
    expense_draft,
    expense_response,
    refund_draft,
    settlement_draft,
    settlement_response,
    waiver_draft,
)
from beluno.contracts.finance import (
    BudgetCreateRequest,
    BudgetResponse,
    BudgetUpdateRequest,
    CommitmentCreateRequest,
    CommitmentResponse,
    CommitmentUpdateRequest,
    ExpenseCreateRequest,
    ExpenseRequest,
    ExpenseResponse,
    RefundRequest,
    SettlementRequest,
    SettlementResponse,
    WaiverRequest,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance import budgets, commitments, expenses, settlements
from beluno.modules.iam.rate_limits import FINANCE_WRITES_PER_PLAN
from beluno.sync.commands import Command, CommandCall, EmptyPayload, version_of

FINANCE_FEATURE = "finance"


async def _create_expense(
    ctx: CommandContext, call: CommandCall, body: ExpenseCreateRequest
) -> ExpenseResponse:
    view = await expenses.create_expense(ctx, call.id("plan_id"), body.id, expense_draft(body))
    return expense_response(view)


async def _revise_expense(
    ctx: CommandContext, call: CommandCall, body: ExpenseRequest
) -> ExpenseResponse:
    view = await expenses.revise_expense(
        ctx, call.id("plan_id"), call.id("expense_id"), expense_draft(body), required_version(call)
    )
    return expense_response(view)


async def _void_expense(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> ExpenseResponse:
    view = await expenses.void_expense(
        ctx, call.id("plan_id"), call.id("expense_id"), required_version(call)
    )
    return expense_response(view)


async def _refund_expense(
    ctx: CommandContext, call: CommandCall, body: RefundRequest
) -> ExpenseResponse:
    view = await expenses.refund_expense(
        ctx, call.id("plan_id"), call.id("expense_id"), refund_draft(body), required_version(call)
    )
    return expense_response(view)


async def _record_settlement(
    ctx: CommandContext, call: CommandCall, body: SettlementRequest
) -> SettlementResponse:
    view = await settlements.record_settlement(ctx, call.id("plan_id"), settlement_draft(body))
    return settlement_response(view)


async def _waive(ctx: CommandContext, call: CommandCall, body: WaiverRequest) -> SettlementResponse:
    view = await settlements.waive_debt(ctx, call.id("plan_id"), waiver_draft(body))
    return settlement_response(view)


async def _confirm(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> SettlementResponse:
    view = await settlements.answer_settlement(
        ctx, call.id("plan_id"), call.id("settlement_id"), confirm=True
    )
    return settlement_response(view)


async def _dispute(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> SettlementResponse:
    view = await settlements.answer_settlement(
        ctx, call.id("plan_id"), call.id("settlement_id"), confirm=False
    )
    return settlement_response(view)


async def _reverse(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> SettlementResponse:
    view = await settlements.reverse_settlement(
        ctx, call.id("plan_id"), call.id("settlement_id"), required_version(call)
    )
    return settlement_response(view)


async def _create_budget(
    ctx: CommandContext, call: CommandCall, body: BudgetCreateRequest
) -> BudgetResponse:
    draft = budgets.BudgetDraft(
        scope=body.scope,
        category=body.category,
        participant_id=body.participant_id,
        limit_minor=body.limit_minor,
    )
    return budget_response(await budgets.create_budget(ctx, call.id("plan_id"), body.id, draft))


async def _update_budget(
    ctx: CommandContext, call: CommandCall, body: BudgetUpdateRequest
) -> BudgetResponse:
    budget = await budgets.update_budget(
        ctx, call.id("plan_id"), call.id("budget_id"), body.limit_minor, required_version(call)
    )
    return budget_response(budget)


async def _delete_budget(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await budgets.delete_budget(ctx, call.id("plan_id"), call.id("budget_id"))


async def _create_commitment(
    ctx: CommandContext, call: CommandCall, body: CommitmentCreateRequest
) -> CommitmentResponse:
    view = await commitments.create_manual(ctx, call.id("plan_id"), body.id, commitment_draft(body))
    return commitment_response(view)


async def _update_commitment(
    ctx: CommandContext, call: CommandCall, body: CommitmentUpdateRequest
) -> CommitmentResponse:
    view = await commitments.update_manual(
        ctx,
        call.id("plan_id"),
        call.id("commitment_id"),
        commitment_draft(body),
        required_version(call),
    )
    return commitment_response(view)


EXPENSE_CREATE = Command(
    name="expense.create",
    payload_model=ExpenseCreateRequest,
    response_model=ExpenseResponse,
    handler=_create_expense,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
EXPENSE_REVISE = Command(
    name="expense.revise",
    payload_model=ExpenseRequest,
    response_model=ExpenseResponse,
    handler=_revise_expense,
    target_fields=("plan_id", "expense_id"),
    versioned=True,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
EXPENSE_VOID = Command(
    name="expense.void",
    payload_model=EmptyPayload,
    response_model=ExpenseResponse,
    handler=_void_expense,
    target_fields=("plan_id", "expense_id"),
    versioned=True,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
EXPENSE_REFUND = Command(
    name="expense.refund",
    payload_model=RefundRequest,
    response_model=ExpenseResponse,
    handler=_refund_expense,
    target_fields=("plan_id", "expense_id"),
    versioned=True,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)

SETTLEMENT_RECORD = Command(
    name="settlement.record",
    payload_model=SettlementRequest,
    response_model=SettlementResponse,
    handler=_record_settlement,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
SETTLEMENT_WAIVE = Command(
    name="settlement.waive",
    payload_model=WaiverRequest,
    response_model=SettlementResponse,
    handler=_waive,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
SETTLEMENT_CONFIRM = Command(
    name="settlement.confirm",
    payload_model=EmptyPayload,
    response_model=SettlementResponse,
    handler=_confirm,
    target_fields=("plan_id", "settlement_id"),
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
SETTLEMENT_DISPUTE = Command(
    name="settlement.dispute",
    payload_model=EmptyPayload,
    response_model=SettlementResponse,
    handler=_dispute,
    target_fields=("plan_id", "settlement_id"),
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
SETTLEMENT_REVERSE = Command(
    name="settlement.reverse",
    payload_model=EmptyPayload,
    response_model=SettlementResponse,
    handler=_reverse,
    target_fields=("plan_id", "settlement_id"),
    versioned=True,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)

BUDGET_CREATE = Command(
    name="budget.create",
    payload_model=BudgetCreateRequest,
    response_model=BudgetResponse,
    handler=_create_budget,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
BUDGET_UPDATE = Command(
    name="budget.update",
    payload_model=BudgetUpdateRequest,
    response_model=BudgetResponse,
    handler=_update_budget,
    target_fields=("plan_id", "budget_id"),
    versioned=True,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
BUDGET_DELETE = Command(
    name="budget.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_delete_budget,
    target_fields=("plan_id", "budget_id"),
    status=204,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
COMMITMENT_CREATE = Command(
    name="commitment.create",
    payload_model=CommitmentCreateRequest,
    response_model=CommitmentResponse,
    handler=_create_commitment,
    target_fields=("plan_id",),
    status=201,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
COMMITMENT_UPDATE = Command(
    name="commitment.update",
    payload_model=CommitmentUpdateRequest,
    response_model=CommitmentResponse,
    handler=_update_commitment,
    target_fields=("plan_id", "commitment_id"),
    versioned=True,
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)

COMMANDS: list[Command[Any, Any]] = [
    EXPENSE_CREATE,
    EXPENSE_REVISE,
    EXPENSE_VOID,
    EXPENSE_REFUND,
    SETTLEMENT_RECORD,
    SETTLEMENT_WAIVE,
    SETTLEMENT_CONFIRM,
    SETTLEMENT_DISPUTE,
    SETTLEMENT_REVERSE,
    BUDGET_CREATE,
    BUDGET_UPDATE,
    BUDGET_DELETE,
    COMMITMENT_CREATE,
    COMMITMENT_UPDATE,
]
