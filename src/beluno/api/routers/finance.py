"""Plan finance: currencies, expenses with revisions and refunds, and the ledger view.

Beluno records what people spent and owe each other; it never holds or moves money.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from beluno.api.commands import finance as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.finance_presenters import (
    budget_overview_response,
    commitment_response,
    consolidation_response,
    currency_response,
    expense_response,
    explanation_entry,
    fund_count_response,
    fund_movement_response,
    fund_settings_response,
    ledger_response,
    preview_response,
    rate_text,
    revision_response,
    settlement_response,
    transaction_response,
)
from beluno.api.http import (
    DEFAULT_PAGE_SIZE,
    CursorParam,
    IdempotencyKey,
    IfMatch,
    LimitParam,
    command_call,
    decode_cursor,
    encode_cursor,
    finish,
    finish_empty,
    set_etag,
)
from beluno.api.problems import problem_responses
from beluno.contracts.common import Page
from beluno.contracts.errors import validation_error
from beluno.contracts.finance import (
    AdjustmentRequest,
    BalanceExplanation,
    BaseCurrencyRequest,
    BudgetCreateRequest,
    BudgetOverviewResponse,
    BudgetResponse,
    BudgetUpdateRequest,
    CommitmentCreateRequest,
    CommitmentResponse,
    CommitmentUpdateRequest,
    ConsolidateRequest,
    ConsolidationResponse,
    CurrencyResponse,
    ExpenseCreateRequest,
    ExpenseRequest,
    ExpenseResponse,
    FundAvailabilityResponse,
    FundCountRequest,
    FundCountResponse,
    FundMovementRequest,
    FundMovementResponse,
    FundResponse,
    FundSettingsRequest,
    FundSettingsResponse,
    LedgerConfirmRequest,
    LedgerResponse,
    LedgerSettingsRequest,
    MarketRateResponse,
    MarketRatesResponse,
    MemberContribution,
    RefundRequest,
    RevisionResponse,
    SettlementPreviewResponse,
    SettlementRequest,
    SettlementResponse,
    TransactionPage,
    TransactionResponse,
    WaiverRequest,
)
from beluno.contracts.plans import PlanResponse
from beluno.modules.context import open_context
from beluno.modules.finance import (
    budgets,
    commitments,
    consolidation,
    currencies,
    expenses,
    funds,
    market_rates,
    settlements,
    views,
)
from beluno.sync.commands import Command, EmptyPayload

router = APIRouter(prefix="/v1/plans/{plan_id}", tags=["finance"])
currency_router = APIRouter(prefix="/v1/currencies", tags=["finance"])
fx_router = APIRouter(prefix="/v1/fx", tags=["finance"])

READ_ERRORS = problem_responses(401, 403, 404, 422, 503)
CurrencyQuery = Annotated[str, Query(pattern=r"^[A-Z]{3}$")]
CurrencyFilter = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 429, 503)


@currency_router.get("", response_model=list[CurrencyResponse], responses=READ_ERRORS)
async def list_currencies(runtime: RuntimeDep, actor: ActorDep) -> list[CurrencyResponse]:
    """Supported currencies and their pinned minor-unit exponents."""

    async with open_context(runtime, actor) as ctx:
        rows = await currencies.list_currencies(ctx)
    return [currency_response(row) for row in rows]


@fx_router.get("/rates", response_model=MarketRatesResponse, responses=READ_ERRORS)
async def list_market_rates(
    base: CurrencyQuery, runtime: RuntimeDep, actor: ActorDep
) -> MarketRatesResponse:
    """The latest published market rates from ``base``, for offline estimates only."""

    async with open_context(runtime, actor) as ctx:
        rows = await market_rates.latest_rates(ctx, base)
    return MarketRatesResponse(
        base=base,
        rates=[
            MarketRateResponse(
                quote=row.quote_currency,
                rate=rate_text(row.rate),
                as_of=row.as_of,
                source=row.source,
            )
            for row in rows
        ],
    )


@router.post("/base-currency", response_model=PlanResponse, responses=WRITE_ERRORS)
async def change_base_currency(
    plan_id: UUID,
    body: BaseCurrencyRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> PlanResponse:
    """Move the plan to another base currency (owner or admin, plan version in If-Match).

    Original amounts and postings never change; budgets and the settle tolerance are
    re-denominated at the rate, and base values read through the chain of changes.
    """

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.PLAN_CHANGE_BASE_CURRENCY, call, body))


@router.get("/ledger", response_model=LedgerResponse, responses=READ_ERRORS)
async def get_ledger(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> LedgerResponse:
    """Balances per participant and currency (positive: should receive money)."""

    async with open_context(runtime, actor) as ctx:
        snapshot = await views.get_ledger(ctx, plan_id)
    rendered = ledger_response(snapshot)
    set_etag(response, rendered.version)
    return rendered


@router.get("/expenses", response_model=Page[ExpenseResponse], responses=READ_ERRORS)
async def list_expenses(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> Page[ExpenseResponse]:
    async with open_context(runtime, actor) as ctx:
        found = await expenses.list_expenses(ctx, plan_id, after=decode_cursor(cursor), limit=limit)
    next_cursor = encode_cursor(found[-1].expense.id) if len(found) == limit else None
    return Page[ExpenseResponse](
        items=[expense_response(view) for view in found], next_cursor=next_cursor
    )


@router.post(
    "/expenses",
    status_code=status.HTTP_201_CREATED,
    response_model=ExpenseResponse,
    responses=WRITE_ERRORS,
)
async def create_expense(
    plan_id: UUID,
    body: ExpenseCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ExpenseResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.EXPENSE_CREATE, call, body))


@router.get("/expenses/{expense_id}", response_model=ExpenseResponse, responses=READ_ERRORS)
async def get_expense(
    plan_id: UUID, expense_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> ExpenseResponse:
    async with open_context(runtime, actor) as ctx:
        view = await expenses.get_expense(ctx, plan_id, expense_id)
    set_etag(response, view.expense.version)
    return expense_response(view)


@router.put("/expenses/{expense_id}", response_model=ExpenseResponse, responses=WRITE_ERRORS)
async def revise_expense(
    plan_id: UUID,
    expense_id: UUID,
    body: ExpenseRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> ExpenseResponse:
    """Append a new revision; the previous one is reversed, never overwritten.

    While refunds are in effect the currency and split stay as they are.
    """

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, expense_id=expense_id)
    return finish(response, await runner.run(actor, commands.EXPENSE_REVISE, call, body))


@router.post("/expenses/{expense_id}/void", response_model=ExpenseResponse, responses=WRITE_ERRORS)
async def void_expense(
    plan_id: UUID,
    expense_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> ExpenseResponse:
    """Reverse the expense and its refunds; it stays in history as ``voided``."""

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, expense_id=expense_id)
    result = await runner.run(actor, commands.EXPENSE_VOID, call, EmptyPayload())
    return finish(response, result)


@router.post(
    "/expenses/{expense_id}/refunds", response_model=ExpenseResponse, responses=WRITE_ERRORS
)
async def refund_expense(
    plan_id: UUID,
    expense_id: UUID,
    body: RefundRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> ExpenseResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, expense_id=expense_id)
    return finish(response, await runner.run(actor, commands.EXPENSE_REFUND, call, body))


@router.get(
    "/expenses/{expense_id}/revisions",
    response_model=list[RevisionResponse],
    responses=READ_ERRORS,
)
async def list_revisions(
    plan_id: UUID, expense_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> list[RevisionResponse]:
    """Every revision of the expense, oldest first."""

    async with open_context(runtime, actor) as ctx:
        found = await expenses.list_revisions(ctx, plan_id, expense_id)
    return [revision_response(view) for view in found]


def _seq_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if not cursor.isdigit() or len(cursor) > 18:
        raise validation_error("cursor is invalid")
    return int(cursor)


@router.patch("/ledger/settings", response_model=LedgerResponse, responses=WRITE_ERRORS)
async def configure_ledger(
    plan_id: UUID,
    body: LedgerSettingsRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> LedgerResponse:
    """Money settings (managers): count personal spend, and the settled-under tolerance."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.LEDGER_CONFIGURE, call, body))


@router.post("/ledger/confirm", response_model=LedgerResponse, responses=WRITE_ERRORS)
async def confirm_ledger(
    plan_id: UUID,
    body: LedgerConfirmRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> LedgerResponse:
    """Say the ledger at ``ledger_seq`` looks right to you; any later entry makes it stale."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.LEDGER_CONFIRM, call, body))


@router.get(
    "/ledger/consolidations", response_model=list[ConsolidationResponse], responses=READ_ERRORS
)
async def list_consolidations(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> list[ConsolidationResponse]:
    async with open_context(runtime, actor) as ctx:
        found = await consolidation.list_consolidations(ctx, plan_id)
    return [consolidation_response(view) for view in found]


@router.post(
    "/ledger/consolidations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConsolidationResponse,
    responses=WRITE_ERRORS,
)
async def consolidate_ledger(
    plan_id: UUID,
    body: ConsolidateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ConsolidationResponse:
    """Settle everything in the base currency (owner or admin) at rates frozen now."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.LEDGER_CONSOLIDATE, call, body))


@router.post(
    "/ledger/consolidations/{consolidation_id}/reverse",
    response_model=ConsolidationResponse,
    responses=WRITE_ERRORS,
)
async def reverse_consolidation(
    plan_id: UUID,
    consolidation_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> ConsolidationResponse:
    """Undo the latest consolidation exactly, while nobody has settled up since."""

    call = command_call(
        idempotency_key, if_match=if_match, plan_id=plan_id, consolidation_id=consolidation_id
    )
    return finish(
        response,
        await runner.run(actor, commands.LEDGER_REVERSE_CONSOLIDATION, call, EmptyPayload()),
    )


@router.get("/ledger/transactions", response_model=TransactionPage, responses=READ_ERRORS)
async def list_transactions(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> TransactionPage:
    """The journal in ledger order; every entry sums to zero in each currency."""

    async with open_context(runtime, actor) as ctx:
        found = await views.list_transactions(
            ctx, plan_id, after_seq=_seq_cursor(cursor), limit=limit
        )
    next_cursor = str(found[-1].transaction.ledger_seq) if len(found) == limit else None
    return TransactionPage(
        items=[transaction_response(view) for view in found], next_cursor=next_cursor
    )


@router.get("/ledger/explanation", response_model=BalanceExplanation, responses=READ_ERRORS)
async def explain_balance(
    plan_id: UUID,
    currency: CurrencyQuery,
    runtime: RuntimeDep,
    actor: ActorDep,
    participant_id: UUID | None = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> BalanceExplanation:
    """Every entry behind one participant's balance (or the fund's, without participant_id)."""

    async with open_context(runtime, actor) as ctx:
        lines = await views.explain_balance(
            ctx,
            plan_id,
            participant_id=participant_id,
            currency=currency,
            after_seq=_seq_cursor(cursor),
            limit=limit,
        )
    next_cursor = str(lines[-1].transaction.ledger_seq) if len(lines) == limit else None
    return BalanceExplanation(
        participant_id=participant_id,
        fund=participant_id is None,
        currency=currency,
        entries=[explanation_entry(line) for line in lines],
        next_cursor=next_cursor,
    )


@router.get(
    "/ledger/settlement-preview",
    response_model=list[SettlementPreviewResponse],
    responses=READ_ERRORS,
)
async def preview_settlements(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep, currency: CurrencyFilter = None
) -> list[SettlementPreviewResponse]:
    """Suggested transfers per currency that would square everyone; nothing is recorded."""

    async with open_context(runtime, actor) as ctx:
        previews = await views.settlement_preview(ctx, plan_id, currency=currency)
    return [preview_response(preview) for preview in previews]


@router.get("/settlements", response_model=Page[SettlementResponse], responses=READ_ERRORS)
async def list_settlements(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> Page[SettlementResponse]:
    async with open_context(runtime, actor) as ctx:
        found = await settlements.list_settlements(
            ctx, plan_id, after=decode_cursor(cursor), limit=limit
        )
    next_cursor = encode_cursor(found[-1].settlement.id) if len(found) == limit else None
    return Page[SettlementResponse](
        items=[settlement_response(view) for view in found], next_cursor=next_cursor
    )


@router.post(
    "/settlements",
    status_code=status.HTTP_201_CREATED,
    response_model=SettlementResponse,
    responses=WRITE_ERRORS,
)
async def record_settlement(
    plan_id: UUID,
    body: SettlementRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SettlementResponse:
    """Record a payment one participant made to another (Beluno moves no money)."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.SETTLEMENT_RECORD, call, body))


@router.post(
    "/waivers",
    status_code=status.HTTP_201_CREATED,
    response_model=SettlementResponse,
    responses=WRITE_ERRORS,
)
async def waive_debt(
    plan_id: UUID,
    body: WaiverRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SettlementResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.SETTLEMENT_WAIVE, call, body))


@router.get(
    "/settlements/{settlement_id}", response_model=SettlementResponse, responses=READ_ERRORS
)
async def get_settlement(
    plan_id: UUID, settlement_id: UUID, runtime: RuntimeDep, actor: ActorDep, response: Response
) -> SettlementResponse:
    async with open_context(runtime, actor) as ctx:
        view = await settlements.get_settlement(ctx, plan_id, settlement_id)
    set_etag(response, view.settlement.version)
    return settlement_response(view)


async def _settlement_intent(
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    command: Command[EmptyPayload, SettlementResponse],
    plan_id: UUID,
    settlement_id: UUID,
    idempotency_key: str | None,
    if_match: str | None = None,
) -> SettlementResponse:
    call = command_call(
        idempotency_key, if_match=if_match, plan_id=plan_id, settlement_id=settlement_id
    )
    return finish(response, await runner.run(actor, command, call, EmptyPayload()))


@router.post(
    "/settlements/{settlement_id}/confirm",
    response_model=SettlementResponse,
    responses=WRITE_ERRORS,
)
async def confirm_settlement(
    plan_id: UUID,
    settlement_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SettlementResponse:
    """The creditor confirms the money arrived."""

    return await _settlement_intent(
        runner,
        actor,
        response,
        commands.SETTLEMENT_CONFIRM,
        plan_id,
        settlement_id,
        idempotency_key,
    )


@router.post(
    "/settlements/{settlement_id}/dispute",
    response_model=SettlementResponse,
    responses=WRITE_ERRORS,
)
async def dispute_settlement(
    plan_id: UUID,
    settlement_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SettlementResponse:
    """The creditor says the money did not arrive; balances stay until it is reversed."""

    return await _settlement_intent(
        runner,
        actor,
        response,
        commands.SETTLEMENT_DISPUTE,
        plan_id,
        settlement_id,
        idempotency_key,
    )


@router.post(
    "/settlements/{settlement_id}/reverse",
    response_model=SettlementResponse,
    responses=WRITE_ERRORS,
)
async def reverse_settlement(
    plan_id: UUID,
    settlement_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> SettlementResponse:
    """Append the exact reversal of the settlement's entries."""

    return await _settlement_intent(
        runner,
        actor,
        response,
        commands.SETTLEMENT_REVERSE,
        plan_id,
        settlement_id,
        idempotency_key,
        if_match,
    )


@router.get("/budgets", response_model=BudgetOverviewResponse, responses=READ_ERRORS)
async def get_budgets(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> BudgetOverviewResponse:
    """Limits against actual, committed, and estimated spend in the plan's base currency."""

    async with open_context(runtime, actor) as ctx:
        overview = await budgets.get_budgets(ctx, plan_id)
    return budget_overview_response(overview)


@router.post(
    "/budgets",
    status_code=status.HTTP_201_CREATED,
    response_model=BudgetResponse,
    responses=WRITE_ERRORS,
)
async def create_budget(
    plan_id: UUID,
    body: BudgetCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> BudgetResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.BUDGET_CREATE, call, body))


@router.patch("/budgets/{budget_id}", response_model=BudgetResponse, responses=WRITE_ERRORS)
async def update_budget(
    plan_id: UUID,
    budget_id: UUID,
    body: BudgetUpdateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> BudgetResponse:
    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id, budget_id=budget_id)
    return finish(response, await runner.run(actor, commands.BUDGET_UPDATE, call, body))


@router.delete(
    "/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT, responses=WRITE_ERRORS
)
async def delete_budget(
    plan_id: UUID,
    budget_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    call = command_call(idempotency_key, plan_id=plan_id, budget_id=budget_id)
    return finish_empty(await runner.run(actor, commands.BUDGET_DELETE, call, EmptyPayload()))


@router.get("/commitments", response_model=list[CommitmentResponse], responses=READ_ERRORS)
async def list_commitments(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> list[CommitmentResponse]:
    """Planned and committed costs from every module, with the expense that settled each."""

    async with open_context(runtime, actor) as ctx:
        found = await commitments.list_commitments(ctx, plan_id)
    return [commitment_response(view) for view in found]


@router.post(
    "/commitments",
    status_code=status.HTTP_201_CREATED,
    response_model=CommitmentResponse,
    responses=WRITE_ERRORS,
)
async def create_commitment(
    plan_id: UUID,
    body: CommitmentCreateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> CommitmentResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.COMMITMENT_CREATE, call, body))


@router.put(
    "/commitments/{commitment_id}", response_model=CommitmentResponse, responses=WRITE_ERRORS
)
async def update_commitment(
    plan_id: UUID,
    commitment_id: UUID,
    body: CommitmentUpdateRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> CommitmentResponse:
    call = command_call(
        idempotency_key, if_match=if_match, plan_id=plan_id, commitment_id=commitment_id
    )
    return finish(response, await runner.run(actor, commands.COMMITMENT_UPDATE, call, body))


@router.get("/fund", response_model=FundResponse, responses=READ_ERRORS)
async def get_fund(plan_id: UUID, runtime: RuntimeDep, actor: ActorDep) -> FundResponse:
    """What participants pooled with a custodian; Beluno holds and moves no money."""

    async with open_context(runtime, actor) as ctx:
        settings = await funds.get_settings(ctx, plan_id)
        available = await views.fund_availability(ctx, plan_id)
        contributions = await funds.contributions(ctx, plan_id)
        counts = await funds.latest_counts(ctx, plan_id)
    return FundResponse(
        settings=fund_settings_response(settings) if settings else None,
        available=[
            FundAvailabilityResponse(currency=currency, available_minor=amount)
            for currency, amount in available
        ],
        contributions=[
            MemberContribution(
                participant_id=row.participant_id,
                currency=row.currency,
                contributed_minor=row.contributed_minor,
            )
            for row in contributions
        ],
        counts=[fund_count_response(count) for count in counts],
    )


@router.put("/fund", response_model=FundSettingsResponse, responses=WRITE_ERRORS)
async def put_fund(
    plan_id: UUID,
    body: FundSettingsRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> FundSettingsResponse:
    """Create the fund settings, or replace them with the current ``If-Match`` version."""

    call = command_call(idempotency_key, if_match=if_match, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.FUND_UPDATE, call, body))


@router.get("/fund/movements", response_model=Page[FundMovementResponse], responses=READ_ERRORS)
async def list_fund_movements(
    plan_id: UUID,
    runtime: RuntimeDep,
    actor: ActorDep,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_SIZE,
) -> Page[FundMovementResponse]:
    async with open_context(runtime, actor) as ctx:
        found = await funds.list_movements(ctx, plan_id, after=decode_cursor(cursor), limit=limit)
    next_cursor = encode_cursor(found[-1].id) if len(found) == limit else None
    return Page[FundMovementResponse](
        items=[fund_movement_response(row) for row in found], next_cursor=next_cursor
    )


@router.post(
    "/fund/contributions",
    status_code=status.HTTP_201_CREATED,
    response_model=FundMovementResponse,
    responses=WRITE_ERRORS,
)
async def contribute_to_fund(
    plan_id: UUID,
    body: FundMovementRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> FundMovementResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.FUND_CONTRIBUTE, call, body))


@router.post(
    "/fund/withdrawals",
    status_code=status.HTTP_201_CREATED,
    response_model=FundMovementResponse,
    responses=WRITE_ERRORS,
)
async def withdraw_from_fund(
    plan_id: UUID,
    body: FundMovementRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> FundMovementResponse:
    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.FUND_WITHDRAW, call, body))


@router.post(
    "/fund/counts",
    status_code=status.HTTP_201_CREATED,
    response_model=FundCountResponse,
    responses=WRITE_ERRORS,
)
async def count_fund(
    plan_id: UUID,
    body: FundCountRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> FundCountResponse:
    """The custodian or a manager records the cash counted; nothing is posted."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.FUND_COUNT, call, body))


@router.post(
    "/ledger/adjustments",
    status_code=status.HTTP_201_CREATED,
    response_model=TransactionResponse,
    responses=WRITE_ERRORS,
)
async def adjust_ledger(
    plan_id: UUID,
    body: AdjustmentRequest,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> TransactionResponse:
    """A privileged correction (owner, recent sign-in); it is audited and never edits history."""

    call = command_call(idempotency_key, plan_id=plan_id)
    return finish(response, await runner.run(actor, commands.LEDGER_ADJUST, call, body))
