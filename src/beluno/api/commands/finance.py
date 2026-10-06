"""Finance commands: expenses, settlements, budgets, commitments, the fund, adjustments."""

from __future__ import annotations

from typing import Any

from beluno.api.finance_presenters import (
    adjustment_draft,
    budget_response,
    commitment_draft,
    commitment_response,
    expense_draft,
    expense_response,
    fund_movement_response,
    fund_settings_response,
    ledger_response,
    movement_draft,
    refund_draft,
    settlement_draft,
    settlement_response,
    transaction_response,
    waiver_draft,
)
from beluno.contracts.finance import (
    AdjustmentRequest,
    BudgetCreateRequest,
    BudgetResponse,
    BudgetUpdateRequest,
    CommitmentCreateRequest,
    CommitmentResponse,
    CommitmentUpdateRequest,
    ExpenseCreateRequest,
    ExpenseRequest,
    ExpenseResponse,
    FundMovementRequest,
    FundMovementResponse,
    FundSettingsRequest,
    FundSettingsResponse,
    LedgerConfirmRequest,
    LedgerResponse,
    LedgerSettingsRequest,
    RefundRequest,
    SettlementRequest,
    SettlementResponse,
    TransactionResponse,
    WaiverRequest,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance import (
    budgets,
    commitments,
    expenses,
    funds,
    ledger_settings,
    settlements,
    views,
)
from beluno.modules.finance.expenses import RevisionOrigin
from beluno.modules.iam.rate_limits import FINANCE_WRITES_PER_PLAN
from beluno.sync.commands import Command, CommandCall, EmptyPayload, required_version, version_of

FINANCE_FEATURE = "finance"


def _origin(call: CommandCall) -> RevisionOrigin:
    source = expenses.SYNC if call.source == "push" else expenses.HTTP
    return RevisionOrigin(source=source, client_created_at=call.client_created_at)


async def _create_expense(
    ctx: CommandContext, call: CommandCall, body: ExpenseCreateRequest
) -> ExpenseResponse:
    view = await expenses.create_expense(
        ctx, call.id("plan_id"), body.id, expense_draft(body, _origin(call))
    )
    return expense_response(view)


async def _revise_expense(
    ctx: CommandContext, call: CommandCall, body: ExpenseRequest
) -> ExpenseResponse:
    view = await expenses.revise_expense(
        ctx,
        call.id("plan_id"),
        call.id("expense_id"),
        expense_draft(body, _origin(call)),
        required_version(call),
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


async def _put_fund(
    ctx: CommandContext, call: CommandCall, body: FundSettingsRequest
) -> FundSettingsResponse:
    # Creation needs no version; replacing the settings requires the current one.
    settings = await funds.put_settings(
        ctx,
        call.id("plan_id"),
        custodian_participant_id=body.custodian_participant_id,
        note=body.note,
        expected_version=call.expected_version,
    )
    return fund_settings_response(settings)


async def _contribute(
    ctx: CommandContext, call: CommandCall, body: FundMovementRequest
) -> FundMovementResponse:
    movement = await funds.contribute(ctx, call.id("plan_id"), movement_draft(body))
    return fund_movement_response(movement)


async def _withdraw(
    ctx: CommandContext, call: CommandCall, body: FundMovementRequest
) -> FundMovementResponse:
    movement = await funds.withdraw(ctx, call.id("plan_id"), movement_draft(body))
    return fund_movement_response(movement)


async def _adjust(
    ctx: CommandContext, call: CommandCall, body: AdjustmentRequest
) -> TransactionResponse:
    transaction = await funds.adjust_ledger(ctx, call.id("plan_id"), adjustment_draft(body))
    return transaction_response(await views.transaction_view(ctx, transaction))


async def _configure_ledger(
    ctx: CommandContext, call: CommandCall, body: LedgerSettingsRequest
) -> LedgerResponse:
    draft = ledger_settings.LedgerSettingsDraft(
        count_personal_spend=body.count_personal_spend,
        settle_tolerance_minor=body.settle_tolerance_minor,
    )
    return ledger_response(await ledger_settings.configure_ledger(ctx, call.id("plan_id"), draft))


async def _confirm_ledger(
    ctx: CommandContext, call: CommandCall, body: LedgerConfirmRequest
) -> LedgerResponse:
    snapshot = await ledger_settings.confirm_ledger(ctx, call.id("plan_id"), body.ledger_seq)
    return ledger_response(snapshot)


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

FUND_UPDATE = Command(
    name="fund.update",
    payload_model=FundSettingsRequest,
    response_model=FundSettingsResponse,
    handler=_put_fund,
    target_fields=("plan_id",),
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
FUND_CONTRIBUTE = Command(
    name="fund.contribute",
    payload_model=FundMovementRequest,
    response_model=FundMovementResponse,
    handler=_contribute,
    target_fields=("plan_id",),
    status=201,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
FUND_WITHDRAW = Command(
    name="fund.withdraw",
    payload_model=FundMovementRequest,
    response_model=FundMovementResponse,
    handler=_withdraw,
    target_fields=("plan_id",),
    status=201,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
LEDGER_ADJUST = Command(
    name="ledger.adjust",
    payload_model=AdjustmentRequest,
    response_model=TransactionResponse,
    handler=_adjust,
    target_fields=("plan_id",),
    status=201,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)

LEDGER_CONFIGURE = Command(
    name="ledger.configure",
    payload_model=LedgerSettingsRequest,
    response_model=LedgerResponse,
    handler=_configure_ledger,
    target_fields=("plan_id",),
    etag=version_of,
    feature=FINANCE_FEATURE,
    rate_limit=FINANCE_WRITES_PER_PLAN,
    rate_limit_target="plan_id",
)
LEDGER_CONFIRM = Command(
    name="ledger.confirm",
    payload_model=LedgerConfirmRequest,
    response_model=LedgerResponse,
    handler=_confirm_ledger,
    target_fields=("plan_id",),
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
    FUND_UPDATE,
    FUND_CONTRIBUTE,
    FUND_WITHDRAW,
    LEDGER_ADJUST,
    LEDGER_CONFIGURE,
    LEDGER_CONFIRM,
]
