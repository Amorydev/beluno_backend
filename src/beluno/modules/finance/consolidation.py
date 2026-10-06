"""Settle everything in the base currency ("All in VND").

A consolidation freezes one rate per foreign currency that still has open
balances and appends one ``conversion`` entry that moves every participant's
balance in those currencies into the base currency. Each currency's balances
sum to zero (the kitty must be empty in it first), and the base amounts are
allocated with the largest-remainder rule on each side, so the entry stays
zero-sum in every currency. Immutable lines record what each person moved and
what it became; the database checks the entry posts exactly those lines.

After it, balances, the preview, and settlements are plain base-currency ones;
later foreign expenses open new foreign balances again. The latest
consolidation can be reversed (an exact ``conversion_reversal``) until someone
records a payment or waiver after it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import (
    conflict,
    invalid_state,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.finance import (
    Consolidation,
    ConsolidationLine,
    ConsolidationRate,
    FxSnapshot,
    LedgerTransaction,
    Settlement,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.errors import base_currency_changed
from beluno.modules.finance.fx import convert, parse_rate
from beluno.modules.finance.ledger import Ledger, open_ledger
from beluno.modules.finance.postings import FUND, Party, Postings, consolidation_amounts
from beluno.modules.finance.rates import RateInput, record_rate
from beluno.modules.finance.states import SettlementStatus
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

CONSOLIDATION_ENTITY = "consolidation"
ACTIVE = "active"
REVERSED = "reversed"


@dataclass(frozen=True)
class ConsolidationView:
    consolidation: Consolidation
    rates: list[tuple[ConsolidationRate, FxSnapshot]]
    lines: list[ConsolidationLine]


# --- reads ---------------------------------------------------------------------------


async def list_consolidations(ctx: CommandContext, plan_id: UUID) -> list[ConsolidationView]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    rows = await ctx.session.execute(
        select(Consolidation).where(Consolidation.plan_id == plan_id).order_by(Consolidation.id)
    )
    return [await consolidation_view(ctx, row) for row in rows.scalars()]


async def consolidation_view(
    ctx: CommandContext, consolidation: Consolidation
) -> ConsolidationView:
    rates = await ctx.session.execute(
        select(ConsolidationRate, FxSnapshot)
        .join(FxSnapshot, FxSnapshot.id == ConsolidationRate.fx_snapshot_id)
        .where(ConsolidationRate.consolidation_id == consolidation.id)
        .order_by(ConsolidationRate.currency)
    )
    lines = await ctx.session.execute(
        select(ConsolidationLine)
        .where(ConsolidationLine.consolidation_id == consolidation.id)
        .order_by(ConsolidationLine.currency, ConsolidationLine.participant_id)
    )
    return ConsolidationView(
        consolidation=consolidation,
        rates=[(rate, snapshot) for rate, snapshot in rates.all()],
        lines=list(lines.scalars()),
    )


# --- writes --------------------------------------------------------------------------


async def consolidate(
    ctx: CommandContext,
    plan_id: UUID,
    consolidation_id: UUID | None,
    base_currency: str,
    rates: Mapping[str, RateInput],
) -> ConsolidationView:
    """Convert every foreign-currency balance into ``base_currency`` at the given rates."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.CONSOLIDATE_LEDGER)
    base = ledger.access.plan.base_currency
    if base_currency != base:
        raise base_currency_changed()
    open_balances = _foreign_balances(ledger, base)
    if not open_balances:
        raise invalid_state("Every balance is already in the base currency")
    missing, extra = set(open_balances) - set(rates), set(rates) - set(open_balances)
    if missing or extra:
        raise validation_error(
            "send one rate for each currency with open balances: "
            + ", ".join(sorted(open_balances))
        )
    for currency in open_balances:
        if ledger.balance_of(FUND, currency) != 0:
            raise conflict(
                "FUND_NOT_EMPTY",
                "The kitty still holds money in a currency to convert",
                "Pay the kitty out first, then settle everything in the base currency",
            )
    actor = ctx.require_actor().user_id
    consolidation = Consolidation(
        id=consolidation_id or new_id(),
        plan_id=plan_id,
        base_currency=base,
        state=ACTIVE,
        created_by_user_id=actor,
        created_at=ctx.now,
        reversed_by_user_id=None,
        reversed_at=None,
        version=1,
        updated_at=ctx.now,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(consolidation)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    base_exponent = (await ledger.currency(base)).exponent
    postings: dict[str, Postings] = {base: {}}
    base_totals: dict[Party, int] = defaultdict(int)
    for currency in sorted(open_balances):
        balances = open_balances[currency]
        rate = parse_rate(rates[currency].rate)
        snapshot = await record_rate(
            ledger,
            base=currency,
            quote=base,
            rate=rate,
            source=rates[currency].source,
            as_of=rates[currency].as_of,
        )
        ctx.session.add(
            ConsolidationRate(
                consolidation_id=consolidation.id,
                currency=currency,
                plan_id=plan_id,
                fx_snapshot_id=snapshot.id,
            )
        )
        await ctx.session.flush()
        converted_total = convert(
            sum(value for value in balances.values() if value > 0),
            from_exponent=(await ledger.currency(currency)).exponent,
            to_exponent=base_exponent,
            rate=rate,
        )
        base_amounts = consolidation_amounts(balances, converted_total)
        for participant_id, balance in balances.items():
            ctx.session.add(
                ConsolidationLine(
                    consolidation_id=consolidation.id,
                    currency=currency,
                    participant_id=participant_id,
                    plan_id=plan_id,
                    amount_minor=balance,
                    base_amount_minor=base_amounts[participant_id],
                )
            )
            base_totals[Party(participant_id)] += base_amounts[participant_id]
        postings[currency] = {Party(pid): -value for pid, value in balances.items()}
    postings[base] = dict(base_totals)
    await ctx.session.flush()
    await ledger.append(kind="conversion", postings=postings, consolidation_id=consolidation.id)
    await ledger.finish()
    await _record(ctx, consolidation, "finance.ledger_consolidated")
    return await consolidation_view(ctx, consolidation)


async def reverse_consolidation(
    ctx: CommandContext, plan_id: UUID, consolidation_id: UUID, expected_version: int
) -> ConsolidationView:
    """Undo the latest consolidation exactly, while nobody has settled up since."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.CONSOLIDATE_LEDGER)
    consolidation = (
        await ctx.session.execute(
            select(Consolidation)
            .where(Consolidation.plan_id == plan_id, Consolidation.id == consolidation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if consolidation is None:
        raise not_found()
    if consolidation.version != expected_version:
        raise version_conflict(consolidation)
    if consolidation.state == REVERSED:
        raise invalid_state("This consolidation is already reversed")
    conversion = (
        await ctx.session.execute(
            select(LedgerTransaction).where(
                LedgerTransaction.consolidation_id == consolidation.id,
                LedgerTransaction.kind == "conversion",
            )
        )
    ).scalar_one()
    later = await ctx.session.execute(
        select(LedgerTransaction.id)
        .join(Consolidation, Consolidation.id == LedgerTransaction.consolidation_id)
        .where(
            LedgerTransaction.plan_id == plan_id,
            LedgerTransaction.kind == "conversion",
            LedgerTransaction.ledger_seq > conversion.ledger_seq,
            Consolidation.state == ACTIVE,
        )
    )
    if later.first() is not None:
        raise invalid_state("Reverse the latest consolidation first")
    if await _settled_since(ctx, plan_id, conversion.ledger_seq):
        raise conflict(
            "CONSOLIDATION_SETTLED",
            "Payments were recorded after this consolidation",
            "Reverse those payments first, or keep the base-currency balances",
        )
    await ledger.reverse(conversion, kind="conversion_reversal")
    consolidation.state = REVERSED
    consolidation.reversed_by_user_id = ctx.require_actor().user_id
    consolidation.reversed_at = ctx.now
    consolidation.version += 1
    consolidation.updated_at = ctx.now
    await ctx.session.flush()
    await ledger.finish()
    await _record(ctx, consolidation, "finance.consolidation_reversed")
    return await consolidation_view(ctx, consolidation)


async def open_consolidation_exists(ctx: CommandContext, plan_id: UUID) -> bool:
    found = await ctx.session.execute(
        select(Consolidation.id).where(
            Consolidation.plan_id == plan_id, Consolidation.state == ACTIVE
        )
    )
    return found.first() is not None


# --- helpers -------------------------------------------------------------------------


def _foreign_balances(ledger: Ledger, base: str) -> dict[str, dict[UUID, int]]:
    """Non-zero participant balances in every currency but the base, per currency."""

    found: dict[str, dict[UUID, int]] = defaultdict(dict)
    for (participant_id, currency), account in ledger.accounts.items():
        balance = ledger.balances[account.id].balance_minor
        if participant_id is not None and currency != base and balance != 0:
            found[currency][participant_id] = balance
    return dict(found)


async def _settled_since(ctx: CommandContext, plan_id: UUID, ledger_seq: int) -> bool:
    """A payment or waiver that is still in effect was recorded after ``ledger_seq``."""

    found = await ctx.session.execute(
        select(LedgerTransaction.id)
        .join(Settlement, Settlement.id == LedgerTransaction.settlement_id)
        .where(
            LedgerTransaction.plan_id == plan_id,
            LedgerTransaction.ledger_seq > ledger_seq,
            LedgerTransaction.reverses_transaction_id.is_(None),
            Settlement.status != SettlementStatus.REVERSED.value,
        )
    )
    return found.first() is not None


async def _record(ctx: CommandContext, consolidation: Consolidation, action: str) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=CONSOLIDATION_ENTITY,
        entity_id=consolidation.id,
        entity_version=consolidation.version,
        scope=ChangeScope.PLAN,
        scope_id=consolidation.plan_id,
        plan_id=consolidation.plan_id,
        metadata={"version": consolidation.version, "state": consolidation.state},
    )
