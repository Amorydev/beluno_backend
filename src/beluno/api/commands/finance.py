"""Finance commands: expenses, refunds, settlements, and waivers."""

from __future__ import annotations

from typing import Any

from beluno.api.commands.groups import required_version
from beluno.api.finance_presenters import (
    expense_draft,
    expense_response,
    refund_draft,
    settlement_draft,
    settlement_response,
    waiver_draft,
)
from beluno.contracts.finance import (
    ExpenseCreateRequest,
    ExpenseRequest,
    ExpenseResponse,
    RefundRequest,
    SettlementRequest,
    SettlementResponse,
    WaiverRequest,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance import expenses, settlements
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
]
