"""Sync projections of finance entities in the plan scope.

Finance is private to the plan's participants: these types are never sent to
the ``reader`` access level (group members browsing a group-visible plan).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from beluno.api.finance_presenters import (
    budget_response,
    commitment_response,
    expense_response,
    fund_movement_response,
    fund_settings_response,
    ledger_response,
    settlement_response,
)
from beluno.db.models.finance import (
    Budget,
    CostCommitment,
    Expense,
    FundMovement,
    FundSettings,
    Settlement,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.commitments import commitment_view
from beluno.modules.finance.expenses import expense_view
from beluno.modules.finance.settlements import settlement_view
from beluno.modules.finance.views import ledger_snapshot
from beluno.sync.pull import SnapshotRow
from beluno.sync.scopes import AccessLevel, ScopeKey

FINANCE_TYPES = (
    "ledger",
    "expense",
    "settlement",
    "budget",
    "cost_commitment",
    "fund",
    "fund_movement",
)


async def load_ledger(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    if id != scope.scope_id:
        return None
    snapshot = await ledger_snapshot(ctx, id)
    return ledger_response(snapshot) if snapshot.head is not None else None


async def load_expense(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    expense = await ctx.session.get(Expense, id)
    if expense is None or expense.plan_id != scope.scope_id:
        return None
    return expense_response(await expense_view(ctx, expense))


async def page_ledger(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    if after is not None:
        return []
    snapshot = await ledger_snapshot(ctx, scope.scope_id)
    if snapshot.head is None:
        return []
    return [SnapshotRow(scope.scope_id, snapshot.head.version, ledger_response(snapshot))]


async def page_expenses(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Expense).where(Expense.plan_id == scope.scope_id)
    if after is not None:
        statement = statement.where(Expense.id > after)
    rows = (await ctx.session.execute(statement.order_by(Expense.id).limit(limit))).scalars()
    return [
        SnapshotRow(row.id, row.version, expense_response(await expense_view(ctx, row)))
        for row in rows
    ]


async def load_settlement(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    settlement = await ctx.session.get(Settlement, id)
    if settlement is None or settlement.plan_id != scope.scope_id:
        return None
    return settlement_response(await settlement_view(ctx, settlement))


async def page_settlements(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Settlement).where(Settlement.plan_id == scope.scope_id)
    if after is not None:
        statement = statement.where(Settlement.id > after)
    rows = (await ctx.session.execute(statement.order_by(Settlement.id).limit(limit))).scalars()
    return [
        SnapshotRow(row.id, row.version, settlement_response(await settlement_view(ctx, row)))
        for row in rows
    ]


async def load_budget(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    budget = await ctx.session.get(Budget, id)
    if budget is None or budget.plan_id != scope.scope_id or budget.deleted_at is not None:
        return None
    return budget_response(budget)


async def page_budgets(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(Budget).where(Budget.plan_id == scope.scope_id, Budget.deleted_at.is_(None))
    if after is not None:
        statement = statement.where(Budget.id > after)
    rows = (await ctx.session.execute(statement.order_by(Budget.id).limit(limit))).scalars()
    return [SnapshotRow(row.id, row.version, budget_response(row)) for row in rows]


async def load_commitment(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    commitment = await ctx.session.get(CostCommitment, id)
    if commitment is None or commitment.plan_id != scope.scope_id:
        return None
    return commitment_response(await commitment_view(ctx, commitment))


async def page_commitments(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(CostCommitment).where(CostCommitment.plan_id == scope.scope_id)
    if after is not None:
        statement = statement.where(CostCommitment.id > after)
    rows = (await ctx.session.execute(statement.order_by(CostCommitment.id).limit(limit))).scalars()
    return [
        SnapshotRow(row.id, row.version, commitment_response(await commitment_view(ctx, row)))
        for row in rows
    ]


async def load_fund(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    settings = await ctx.session.get(FundSettings, id)
    if settings is None or settings.plan_id != scope.scope_id:
        return None
    return fund_settings_response(settings)


async def page_fund(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    settings = await ctx.session.get(FundSettings, scope.scope_id)
    if settings is None or after is not None:
        return []
    return [SnapshotRow(settings.plan_id, settings.version, fund_settings_response(settings))]


async def load_fund_movement(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, id: UUID
) -> BaseModel | None:
    movement = await ctx.session.get(FundMovement, id)
    if movement is None or movement.plan_id != scope.scope_id:
        return None
    return fund_movement_response(movement)


async def page_fund_movements(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, after: UUID | None, limit: int
) -> list[SnapshotRow]:
    statement = select(FundMovement).where(FundMovement.plan_id == scope.scope_id)
    if after is not None:
        statement = statement.where(FundMovement.id > after)
    rows = (await ctx.session.execute(statement.order_by(FundMovement.id).limit(limit))).scalars()
    return [SnapshotRow(row.id, 1, fund_movement_response(row)) for row in rows]
