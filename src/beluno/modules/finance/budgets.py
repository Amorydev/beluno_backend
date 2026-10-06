"""Budgets in the plan's base currency, and the budget/actual view.

Spend is computed on read from canonical rows: the current revision of every
live expense, minus its live refunds, converted with the revision's own
snapshot. Spend in another currency without a snapshot is reported as
``unconverted`` and never added silently. Each cost source counts in exactly one
tier: an expense (actual), else a committed commitment, else an estimate, so a
booking and the expense that paid for it are never counted twice.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import conflict, not_found, validation_error, version_conflict
from beluno.db.ids import new_id
from beluno.db.models.finance import (
    Budget,
    CostCommitment,
    Currency,
    Expense,
    ExpenseRefund,
    ExpenseRevision,
    ExpenseSplit,
    FxSnapshot,
    LedgerTransaction,
    RefundShare,
)
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.finance.fx import RateSource, convert
from beluno.modules.finance.ledger import Ledger, open_ledger
from beluno.modules.finance.money import check_amount
from beluno.modules.finance.states import BudgetTier, CommitmentState, commitment_tier
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

BUDGET_ENTITY = "budget"
SCOPES = frozenset({"total", "category", "participant", "daily"})


@dataclass(frozen=True)
class BudgetDraft:
    scope: str
    category: str | None
    participant_id: UUID | None
    limit_minor: int


@dataclass
class Spend:
    actual: int = 0
    committed: int = 0
    estimated: int = 0

    @property
    def projected(self) -> int:
        return self.actual + self.committed + self.estimated

    def add(self, tier: BudgetTier, amount: int) -> None:
        if tier is BudgetTier.ACTUAL:
            self.actual += amount
        elif tier is BudgetTier.COMMITTED:
            self.committed += amount
        else:
            self.estimated += amount


@dataclass
class BudgetUsage:
    budget: Budget
    spend: Spend
    days: list[tuple[date, int]]

    @property
    def over_limit(self) -> bool:
        if self.budget.scope == "daily":
            return any(amount > self.budget.limit_minor for _, amount in self.days)
        return self.spend.projected > self.budget.limit_minor


@dataclass
class BudgetOverview:
    currency: str
    total: Spend
    categories: dict[str, Spend]
    unconverted: dict[tuple[BudgetTier, str], int]
    estimated_rates: bool
    budgets: list[BudgetUsage] = field(default_factory=list)


# --- writes --------------------------------------------------------------------------


async def create_budget(
    ctx: CommandContext, plan_id: UUID, budget_id: UUID | None, draft: BudgetDraft
) -> Budget:
    ledger = await open_ledger(ctx, plan_id, PlanAction.MANAGE_BUDGETS)
    _check_scope(ledger, draft)
    check_amount(draft.limit_minor, field="limit_minor")
    currency = ledger.access.plan.base_currency
    await ledger.currency(currency)
    budget = Budget(
        id=budget_id or new_id(),
        plan_id=plan_id,
        scope=draft.scope,
        category=draft.category,
        participant_id=draft.participant_id,
        currency=currency,
        limit_minor=draft.limit_minor,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        deleted_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(budget)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict(
            "ALREADY_EXISTS", "A budget with this id or for this scope already exists"
        ) from error
    await _record(ctx, budget, "finance.budget_created")
    return budget


async def update_budget(
    ctx: CommandContext, plan_id: UUID, budget_id: UUID, limit_minor: int, expected_version: int
) -> Budget:
    await open_ledger(ctx, plan_id, PlanAction.MANAGE_BUDGETS)
    budget = await _locked(ctx, plan_id, budget_id)
    if budget.version != expected_version:
        raise version_conflict(budget)
    check_amount(limit_minor, field="limit_minor")
    budget.limit_minor = limit_minor
    budget.version += 1
    budget.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, budget, "finance.budget_updated")
    return budget


async def delete_budget(ctx: CommandContext, plan_id: UUID, budget_id: UUID) -> None:
    await open_ledger(ctx, plan_id, PlanAction.MANAGE_BUDGETS)
    budget = await _locked(ctx, plan_id, budget_id)
    budget.deleted_at = ctx.now
    budget.version += 1
    budget.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, budget, "finance.budget_deleted", operation="delete")


def _check_scope(ledger: Ledger, draft: BudgetDraft) -> None:
    if draft.scope not in SCOPES:
        raise validation_error("unknown budget scope")
    if (draft.scope == "category") != (draft.category is not None):
        raise validation_error("category budgets need a category, and only they take one")
    if (draft.scope == "participant") != (draft.participant_id is not None):
        raise validation_error("participant budgets need a participant, and only they take one")
    if draft.participant_id is not None:
        ledger.participant(draft.participant_id)


# --- the budget view -----------------------------------------------------------------


async def get_budgets(ctx: CommandContext, plan_id: UUID) -> BudgetOverview:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    base_currency = access.plan.base_currency
    currencies = {row.code: row for row in (await ctx.session.execute(select(Currency))).scalars()}
    merged = {
        row.id: row.merged_into_participant_id
        for row in (
            await ctx.session.execute(
                select(PlanParticipant).where(PlanParticipant.plan_id == plan_id)
            )
        ).scalars()
    }
    overview = BudgetOverview(
        currency=base_currency,
        total=Spend(),
        categories=defaultdict(Spend),
        unconverted=defaultdict(int),
        estimated_rates=False,
    )
    per_participant: dict[UUID, int] = defaultdict(int)
    per_day: dict[date, int] = defaultdict(int)
    for revision, rate in await _live_revisions(ctx, plan_id):
        refunds = await _live_refund_shares(ctx, revision.expense_id)
        net = revision.amount_minor - sum(refunds.values())
        consumption: dict[UUID, int] = defaultdict(int)
        for split in (
            await ctx.session.execute(
                select(ExpenseSplit).where(ExpenseSplit.revision_id == revision.id)
            )
        ).scalars():
            consumption[_resolve(merged, split.participant_id)] += split.owed_minor
        for participant_id, amount in refunds.items():
            consumption[_resolve(merged, participant_id)] -= amount

        def to_base(
            amount: int, revision: ExpenseRevision = revision, rate: FxSnapshot | None = rate
        ) -> int | None:
            if revision.currency == base_currency:
                return amount
            if rate is None:
                return None
            return convert(
                amount,
                from_exponent=currencies[revision.currency].exponent,
                to_exponent=currencies[base_currency].exponent,
                rate=rate.rate,
            )

        base_net = to_base(net)
        if base_net is None:
            overview.unconverted[(BudgetTier.ACTUAL, revision.currency)] += net
            continue
        if rate is not None and rate.source == RateSource.ESTIMATED.value:
            overview.estimated_rates = True
        overview.total.add(BudgetTier.ACTUAL, base_net)
        overview.categories[revision.category].add(BudgetTier.ACTUAL, base_net)
        per_day[revision.occurred_on] += base_net
        for participant_id, amount in consumption.items():
            per_participant[participant_id] += to_base(amount) or 0
    for commitment, rate in await _commitments(ctx, plan_id):
        tier = commitment_tier(CommitmentState(commitment.state))
        if tier is None:
            continue
        if commitment.base_amount_minor is None:
            overview.unconverted[(tier, commitment.currency)] += commitment.amount_minor
            continue
        if rate is not None and rate.source == RateSource.ESTIMATED.value:
            overview.estimated_rates = True
        overview.total.add(tier, commitment.base_amount_minor)
        overview.categories[commitment.category].add(tier, commitment.base_amount_minor)
    budgets = await ctx.session.execute(
        select(Budget)
        .where(Budget.plan_id == plan_id, Budget.deleted_at.is_(None))
        .order_by(Budget.scope, Budget.category, Budget.participant_id, Budget.id)
    )
    for budget in budgets.scalars():
        if budget.scope == "total":
            spend = overview.total
            days: list[tuple[date, int]] = []
        elif budget.scope == "category":
            assert budget.category is not None
            spend = overview.categories.get(budget.category, Spend())
            days = []
        elif budget.scope == "participant":
            assert budget.participant_id is not None
            spend = Spend(actual=per_participant.get(budget.participant_id, 0))
            days = []
        else:
            days = sorted(per_day.items())
            spend = Spend(actual=max((amount for _, amount in days), default=0))
        overview.budgets.append(BudgetUsage(budget=budget, spend=spend, days=days))
    return overview


def _resolve(merged: dict[UUID, UUID | None], participant_id: UUID) -> UUID:
    current = participant_id
    for _ in range(16):
        nxt = merged.get(current)
        if nxt is None:
            return current
        current = nxt
    return current


async def _live_revisions(
    ctx: CommandContext, plan_id: UUID
) -> list[tuple[ExpenseRevision, FxSnapshot | None]]:
    rows = await ctx.session.execute(
        select(ExpenseRevision, FxSnapshot)
        .join(Expense, Expense.current_revision_id == ExpenseRevision.id)
        .outerjoin(FxSnapshot, FxSnapshot.id == ExpenseRevision.base_fx_snapshot_id)
        .where(Expense.plan_id == plan_id, Expense.state == "active")
        .order_by(ExpenseRevision.id)
    )
    return [(revision, rate) for revision, rate in rows.all()]


async def _live_refund_shares(ctx: CommandContext, expense_id: UUID) -> dict[UUID, int]:
    reversal = aliased(LedgerTransaction)
    reversed_refunds = (
        select(LedgerTransaction.refund_id)
        .join(reversal, reversal.reverses_transaction_id == LedgerTransaction.id)
        .where(LedgerTransaction.refund_id.is_not(None))
    )
    rows = await ctx.session.execute(
        select(RefundShare.participant_id, RefundShare.amount_minor)
        .join(ExpenseRefund, ExpenseRefund.id == RefundShare.refund_id)
        .where(
            ExpenseRefund.expense_id == expense_id,
            ExpenseRefund.id.not_in(reversed_refunds),
        )
    )
    shares: dict[UUID, int] = defaultdict(int)
    for participant_id, amount in rows.all():
        shares[participant_id] += amount
    return shares


async def _commitments(
    ctx: CommandContext, plan_id: UUID
) -> list[tuple[CostCommitment, FxSnapshot | None]]:
    rows = await ctx.session.execute(
        select(CostCommitment, FxSnapshot)
        .outerjoin(FxSnapshot, FxSnapshot.id == CostCommitment.base_fx_snapshot_id)
        .where(CostCommitment.plan_id == plan_id)
        .order_by(CostCommitment.id)
    )
    return [(commitment, rate) for commitment, rate in rows.all()]


async def _locked(ctx: CommandContext, plan_id: UUID, budget_id: UUID) -> Budget:
    budget = (
        await ctx.session.execute(
            select(Budget)
            .where(Budget.plan_id == plan_id, Budget.id == budget_id, Budget.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if budget is None:
        raise not_found()
    return budget


async def _record(
    ctx: CommandContext, budget: Budget, action: str, *, operation: str = "upsert"
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=BUDGET_ENTITY,
        entity_id=budget.id,
        entity_version=budget.version,
        scope=ChangeScope.PLAN,
        scope_id=budget.plan_id,
        plan_id=budget.plan_id,
        metadata={"version": budget.version},
        operation=operation,
    )
