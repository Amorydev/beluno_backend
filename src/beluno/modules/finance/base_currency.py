"""A changeable base currency, read through a chain of frozen rates.

Original amounts, revisions, postings, and settlements never change. Each
change is an immutable, numbered row with one rate from the old base to the new
one. Expense revisions and cost commitments remember the change number they
were valued at, so any base value is their own snapshot followed by every later
change; amounts already in today's base are taken as they are. Budget limits
and the settle tolerance are plain settings in the base currency, so they are
re-denominated at the change's rate.

Without finance data the base currency simply changes; with it, the change
needs a rate, and an open consolidation (not reversed, ledger not settled) must
be finished first because its conversion targeted the old base.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import conflict, invalid_state, validation_error, version_conflict
from beluno.db.models.finance import (
    BaseCurrencyChange,
    Budget,
    Currency,
    LedgerHead,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.consolidation import open_consolidation_exists
from beluno.modules.finance.currencies import require_supported_currency
from beluno.modules.finance.fx import RateSource, convert, parse_rate
from beluno.modules.finance.ledger import ledger_for
from beluno.modules.finance.ledger_settings import MAX_SETTLE_TOLERANCE
from beluno.modules.finance.money import MAX_AMOUNT_MINOR
from beluno.modules.finance.rates import RateInput, record_rate
from beluno.modules.finance.states import LedgerStatus
from beluno.modules.finance.views import list_base_changes
from beluno.modules.plans.changes import bump, record_plan_change
from beluno.modules.plans.service import PlanView
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation


@dataclass(frozen=True)
class ChainStep:
    number: int
    from_currency: str
    to_currency: str
    rate: Decimal
    estimated: bool


@dataclass(frozen=True)
class BaseChain:
    """Today's base currency and every change that led to it, in order."""

    current: str
    steps: Sequence[ChainStep]
    exponents: Mapping[str, int]

    def value(
        self, amount: int, currency: str, *, origin: str, number: int, rate: Decimal | None
    ) -> int | None:
        """``amount`` in ``currency``, valued in base ``origin`` (at ``rate``) after
        ``number`` changes, in today's base currency; None when no rate is known."""

        if currency == self.current:
            return amount
        if currency != origin:
            if rate is None:
                return None
            amount = self._convert(amount, currency, origin, rate)
        for step in self.steps[number:]:
            amount = self._convert(amount, step.from_currency, step.to_currency, step.rate)
        return amount

    def estimated_after(self, number: int) -> bool:
        return any(step.estimated for step in self.steps[number:])

    def origin(self, number: int) -> str:
        """The base currency after ``number`` changes."""

        return self.steps[number].from_currency if number < len(self.steps) else self.current

    def _convert(self, amount: int, source: str, target: str, rate: Decimal) -> int:
        return convert(
            amount,
            from_exponent=self.exponents[source],
            to_exponent=self.exponents[target],
            rate=rate,
        )


async def base_chain(ctx: CommandContext, plan_id: UUID, current: str) -> BaseChain:
    steps = [
        ChainStep(
            change.change_number,
            change.from_currency,
            change.to_currency,
            snapshot.rate,
            snapshot.source == RateSource.ESTIMATED.value,
        )
        for change, snapshot in await list_base_changes(ctx, plan_id)
    ]
    exponents = {
        code: exponent
        for code, exponent in (
            await ctx.session.execute(select(Currency.code, Currency.exponent))
        ).all()
    }
    return BaseChain(current=current, steps=steps, exponents=exponents)


async def base_currency_at(ctx: CommandContext, plan_id: UUID, number: int, current: str) -> str:
    """The plan's base currency after ``number`` changes (``current`` after the last)."""

    after = await ctx.session.get(BaseCurrencyChange, (plan_id, number + 1))
    return after.from_currency if after is not None else current


async def change_base_currency(
    ctx: CommandContext,
    plan_id: UUID,
    expected_version: int,
    currency: str,
    rate: RateInput | None,
) -> PlanView:
    """Move the plan to another base currency (owner or admin)."""

    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.CHANGE_BASE_CURRENCY)
    plan = access.plan
    if plan.version != expected_version:
        raise version_conflict(plan)
    await require_supported_currency(ctx, currency)
    previous = plan.base_currency
    if currency == previous:
        raise invalid_state("This is already the plan's base currency")
    if await ctx.session.get(LedgerHead, plan_id) is not None:
        ledger = await ledger_for(ctx, access)
        if rate is None:
            raise validation_error(
                "a rate from the current base currency is required once the plan has finance data"
            )
        if (
            await open_consolidation_exists(ctx, plan_id)
            and ledger.head.status != LedgerStatus.SETTLED.value
        ):
            raise conflict(
                "CONSOLIDATION_OPEN",
                "Balances were settled in the current base currency",
                "Settle up or reverse that consolidation before changing the base currency",
            )
        frozen = parse_rate(rate.rate)
        snapshot = await record_rate(
            ledger,
            base=previous,
            quote=currency,
            rate=frozen,
            source=rate.source,
            as_of=rate.as_of,
        )
        head = ledger.head
        head.base_change_count += 1
        ctx.session.add(
            BaseCurrencyChange(
                plan_id=plan_id,
                change_number=head.base_change_count,
                from_currency=previous,
                to_currency=currency,
                fx_snapshot_id=snapshot.id,
                ledger_seq=head.ledger_seq,
                created_by_user_id=ctx.require_actor().user_id,
                created_at=ctx.now,
            )
        )
        exponents = (
            (await ledger.currency(previous)).exponent,
            (await ledger.currency(currency)).exponent,
        )
        head.settle_tolerance_minor = min(
            _rebase(head.settle_tolerance_minor, exponents, frozen, minimum=0),
            MAX_SETTLE_TOLERANCE,
        )
        plan.base_currency = currency
        await ctx.session.flush()
        await _rebase_budgets(ctx, plan_id, currency, exponents, frozen)
        await ledger.update_status()
        await ledger.touch()
    else:
        plan.base_currency = currency
    bump(plan, ctx)
    await ctx.session.flush()
    await record_plan_change(
        ctx, plan, "plan.base_currency_changed", {"from": previous, "to": currency}
    )
    return PlanView(plan=plan, participant=access.participant)


def _rebase(amount: int, exponents: tuple[int, int], rate: Decimal, *, minimum: int) -> int:
    converted = convert(amount, from_exponent=exponents[0], to_exponent=exponents[1], rate=rate)
    return min(max(converted, minimum), MAX_AMOUNT_MINOR)


async def _rebase_budgets(
    ctx: CommandContext,
    plan_id: UUID,
    currency: str,
    exponents: tuple[int, int],
    rate: Decimal,
) -> None:
    budgets = await ctx.session.execute(
        select(Budget)
        .where(Budget.plan_id == plan_id, Budget.deleted_at.is_(None))
        .order_by(Budget.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for budget in budgets.scalars():
        budget.limit_minor = _rebase(budget.limit_minor, exponents, rate, minimum=1)
        budget.currency = currency
        budget.version += 1
        budget.updated_at = ctx.now
        await ctx.session.flush()
        await record_mutation(
            ctx,
            action="finance.budget_rebased",
            entity_type="budget",
            entity_id=budget.id,
            entity_version=budget.version,
            scope=ChangeScope.PLAN,
            scope_id=plan_id,
            plan_id=plan_id,
            metadata={"version": budget.version},
        )
