"""Plan finance: currencies, expenses with revisions and refunds, and the ledger view.

Beluno records what people spent and owe each other; it never holds or moves money.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from beluno.api.commands import finance as commands
from beluno.api.dependencies import ActorDep, RunnerDep, RuntimeDep
from beluno.api.finance_presenters import (
    currency_response,
    expense_response,
    ledger_response,
    revision_response,
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
    set_etag,
)
from beluno.api.problems import problem_responses
from beluno.contracts.common import Page
from beluno.contracts.finance import (
    CurrencyResponse,
    ExpenseCreateRequest,
    ExpenseRequest,
    ExpenseResponse,
    LedgerResponse,
    RefundRequest,
    RevisionResponse,
)
from beluno.modules.context import open_context
from beluno.modules.finance import currencies, expenses, views
from beluno.sync.commands import EmptyPayload

router = APIRouter(prefix="/v1/plans/{plan_id}", tags=["finance"])
currency_router = APIRouter(prefix="/v1/currencies", tags=["finance"])

READ_ERRORS = problem_responses(401, 403, 404, 503)
WRITE_ERRORS = problem_responses(401, 403, 404, 409, 412, 422, 428, 429, 503)


@currency_router.get("", response_model=list[CurrencyResponse], responses=READ_ERRORS)
async def list_currencies(runtime: RuntimeDep, actor: ActorDep) -> list[CurrencyResponse]:
    """Supported currencies and their pinned minor-unit exponents."""

    async with open_context(runtime, actor) as ctx:
        rows = await currencies.list_currencies(ctx)
    return [currency_response(row) for row in rows]


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
    """Append a new revision; the previous one is reversed, never overwritten."""

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
