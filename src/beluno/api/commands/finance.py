"""Finance commands: expenses and refunds (settlements, budgets, and fund follow)."""

from __future__ import annotations

from typing import Any

from beluno.api.commands.groups import required_version
from beluno.api.finance_presenters import expense_draft, expense_response, refund_draft
from beluno.contracts.finance import (
    ExpenseCreateRequest,
    ExpenseRequest,
    ExpenseResponse,
    RefundRequest,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance import expenses
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

COMMANDS: list[Command[Any, Any]] = [
    EXPENSE_CREATE,
    EXPENSE_REVISE,
    EXPENSE_VOID,
    EXPENSE_REFUND,
]
