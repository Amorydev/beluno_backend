"""Settlements and waivers: recorded payments between participants, per currency.

Postings are written when a settlement is recorded, so offline devices converge
without waiting for the other party. The creditor (who should have received the
money) confirms or disputes it; managers answer for placeholder creditors, and a
settlement the creditor records is confirmed from the start. Reversal appends the
exact negation of every transaction the settlement wrote.

Paying a debt in another currency records the payment in the paid currency plus
a ``conversion`` that is zero-sum in each currency, at the rate the two agreed
amounts imply; currencies are never netted silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import Decision, PlanAction, decide_plan
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.finance import FxSnapshot, LedgerTransaction, Settlement
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.finance.fx import RateSource, implied_rate
from beluno.modules.finance.ledger import Ledger, open_ledger
from beluno.modules.finance.money import check_amount
from beluno.modules.finance.postings import Party, transfer_postings
from beluno.modules.finance.rates import record_rate
from beluno.modules.finance.states import SETTLEMENT_TRANSITIONS, SettlementStatus
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

SETTLEMENT_ENTITY = "settlement"
PAYMENT = "payment"
WAIVER = "waiver"


@dataclass(frozen=True)
class SettlementDraft:
    settlement_id: UUID | None
    from_participant_id: UUID
    to_participant_id: UUID
    currency: str
    amount_minor: int
    paid_currency: str | None
    paid_amount_minor: int | None
    method: str | None
    fee_minor: int | None
    note: str | None
    occurred_on: date


@dataclass(frozen=True)
class WaiverDraft:
    settlement_id: UUID | None
    debtor_participant_id: UUID
    creditor_participant_id: UUID
    currency: str
    amount_minor: int
    note: str | None
    occurred_on: date


@dataclass(frozen=True)
class SettlementView:
    settlement: Settlement
    rate: FxSnapshot | None


# --- reads ---------------------------------------------------------------------------


async def get_settlement(ctx: CommandContext, plan_id: UUID, settlement_id: UUID) -> SettlementView:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    settlement = await _find(ctx, plan_id, settlement_id)
    if settlement is None:
        raise not_found()
    return await settlement_view(ctx, settlement)


async def list_settlements(
    ctx: CommandContext, plan_id: UUID, *, after: UUID | None, limit: int
) -> list[SettlementView]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    statement = select(Settlement).where(Settlement.plan_id == plan_id)
    if after is not None:
        statement = statement.where(Settlement.id > after)
    rows = await ctx.session.execute(statement.order_by(Settlement.id).limit(limit))
    return [await settlement_view(ctx, row) for row in rows.scalars()]


async def settlement_view(ctx: CommandContext, settlement: Settlement) -> SettlementView:
    rate = (
        await ctx.session.get(FxSnapshot, settlement.fx_snapshot_id)
        if settlement.fx_snapshot_id
        else None
    )
    return SettlementView(settlement=settlement, rate=rate)


# --- writes --------------------------------------------------------------------------


async def record_settlement(
    ctx: CommandContext, plan_id: UUID, draft: SettlementDraft
) -> SettlementView:
    ledger = await open_ledger(ctx, plan_id, PlanAction.RECORD_SETTLEMENT)
    debtor = ledger.settling_party(draft.from_participant_id)
    creditor = ledger.settling_party(draft.to_participant_id)
    if debtor.id == creditor.id:
        raise validation_error("a settlement needs two different participants")
    own = _own_participant(ledger)
    if own not in (debtor.id, creditor.id) and not _manages_settlements(ledger):
        raise forbidden("Record settlements you paid or received, or ask a plan manager")
    check_amount(draft.amount_minor, field="amount_minor")
    currency = await ledger.currency(draft.currency)
    if draft.fee_minor is not None:
        check_amount(draft.fee_minor, field="fee_minor")
    rate = None
    if draft.paid_currency is not None:
        if draft.paid_amount_minor is None or draft.paid_currency == draft.currency:
            raise validation_error("paid must name another currency and its amount")
        check_amount(draft.paid_amount_minor, field="paid amount_minor")
        paid = await ledger.currency(draft.paid_currency)
        rate = await record_rate(
            ledger,
            base=draft.currency,
            quote=draft.paid_currency,
            rate=implied_rate(
                draft.amount_minor,
                from_exponent=currency.exponent,
                to_minor=draft.paid_amount_minor,
                to_exponent=paid.exponent,
            ),
            source=RateSource.AGREED,
            as_of=None,
        )
    settlement = _new_settlement(
        ctx,
        plan_id,
        draft.settlement_id,
        kind=PAYMENT,
        debtor=debtor,
        creditor=creditor,
        currency=draft.currency,
        amount_minor=draft.amount_minor,
        confirmed=own == creditor.id,
        note=draft.note,
        occurred_on=draft.occurred_on,
    )
    settlement.paid_currency = draft.paid_currency
    settlement.paid_amount_minor = draft.paid_amount_minor if rate else None
    settlement.fx_snapshot_id = rate.id if rate else None
    settlement.method = draft.method
    settlement.fee_minor = draft.fee_minor
    await _insert(ctx, settlement)
    payer, receiver = Party(debtor.id), Party(creditor.id)
    if draft.paid_currency is not None and draft.paid_amount_minor is not None:
        paid_amount = draft.paid_amount_minor
        await ledger.append(
            kind="settlement",
            postings={draft.paid_currency: transfer_postings(payer, receiver, paid_amount)},
            settlement_id=settlement.id,
        )
        await ledger.append(
            kind="conversion",
            postings={
                draft.currency: transfer_postings(payer, receiver, draft.amount_minor),
                draft.paid_currency: transfer_postings(receiver, payer, paid_amount),
            },
            settlement_id=settlement.id,
        )
    else:
        await ledger.append(
            kind="settlement",
            postings={draft.currency: transfer_postings(payer, receiver, draft.amount_minor)},
            settlement_id=settlement.id,
        )
    settlement.overpaid = ledger.balance_of(payer, draft.currency) > 0
    await ctx.session.flush()
    await ledger.finish()
    await _record(ctx, settlement, "finance.settlement_recorded")
    return await settlement_view(ctx, settlement)


async def waive_debt(ctx: CommandContext, plan_id: UUID, draft: WaiverDraft) -> SettlementView:
    """The creditor forgives part of a debt; no money changes hands."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.RECORD_SETTLEMENT)
    debtor = ledger.settling_party(draft.debtor_participant_id)
    creditor = ledger.settling_party(draft.creditor_participant_id)
    if debtor.id == creditor.id:
        raise validation_error("a waiver needs two different participants")
    _require_creditor(ledger, creditor, "Only the person who is owed can waive a debt")
    check_amount(draft.amount_minor, field="amount_minor")
    await ledger.currency(draft.currency)
    settlement = _new_settlement(
        ctx,
        plan_id,
        draft.settlement_id,
        kind=WAIVER,
        debtor=debtor,
        creditor=creditor,
        currency=draft.currency,
        amount_minor=draft.amount_minor,
        confirmed=True,
        note=draft.note,
        occurred_on=draft.occurred_on,
    )
    await _insert(ctx, settlement)
    await ledger.append(
        kind="adjustment",
        subtype="waiver",
        postings={
            draft.currency: transfer_postings(
                Party(debtor.id), Party(creditor.id), draft.amount_minor
            )
        },
        settlement_id=settlement.id,
    )
    settlement.overpaid = ledger.balance_of(Party(debtor.id), draft.currency) > 0
    await ctx.session.flush()
    await ledger.finish()
    await _record(ctx, settlement, "finance.debt_waived")
    return await settlement_view(ctx, settlement)


async def answer_settlement(
    ctx: CommandContext, plan_id: UUID, settlement_id: UUID, *, confirm: bool
) -> SettlementView:
    """The creditor confirms receiving the money, or disputes it (an intent command)."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.RECORD_SETTLEMENT)
    settlement = await _locked(ctx, plan_id, settlement_id)
    if settlement.kind == WAIVER:
        raise invalid_state("A waiver needs no confirmation")
    creditor = ledger.participants[settlement.to_participant_id]
    _require_creditor(ledger, creditor, "Only the person who received the money can answer")
    current = SettlementStatus(settlement.status)
    target = SettlementStatus.CONFIRMED if confirm else SettlementStatus.DISPUTED
    if current is target:
        return await settlement_view(ctx, settlement)
    if target not in SETTLEMENT_TRANSITIONS[current]:
        raise invalid_state(f"A {current.value} settlement cannot become {target.value}")
    actor = ctx.require_actor().user_id
    if confirm:
        settlement.confirmed_by_user_id, settlement.confirmed_at = actor, ctx.now
    else:
        settlement.disputed_by_user_id, settlement.disputed_at = actor, ctx.now
    disputes_changed = _move_dispute_count(ledger, current, target)
    settlement.status = target.value
    await _bump(ctx, settlement)
    if disputes_changed:
        await ledger.touch()
    action = "finance.settlement_confirmed" if confirm else "finance.settlement_disputed"
    await _record(ctx, settlement, action)
    return await settlement_view(ctx, settlement)


async def reverse_settlement(
    ctx: CommandContext, plan_id: UUID, settlement_id: UUID, expected_version: int
) -> SettlementView:
    ledger = await open_ledger(ctx, plan_id, PlanAction.RECORD_SETTLEMENT)
    settlement = await _locked(ctx, plan_id, settlement_id)
    actor = ctx.require_actor().user_id
    if settlement.recorded_by_user_id != actor and not _manages_settlements(ledger):
        raise forbidden("Only whoever recorded this settlement or a plan manager can reverse it")
    if settlement.version != expected_version:
        raise version_conflict(settlement)
    current = SettlementStatus(settlement.status)
    if SettlementStatus.REVERSED not in SETTLEMENT_TRANSITIONS[current]:
        raise invalid_state("This settlement is already reversed")
    transactions = await ctx.session.execute(
        select(LedgerTransaction)
        .where(
            LedgerTransaction.settlement_id == settlement.id,
            LedgerTransaction.reverses_transaction_id.is_(None),
        )
        .order_by(LedgerTransaction.ledger_seq.desc())
    )
    for transaction in list(transactions.scalars()):
        await ledger.reverse(transaction, kind="settlement_reversal")
    _move_dispute_count(ledger, current, SettlementStatus.REVERSED)
    settlement.status = SettlementStatus.REVERSED.value
    settlement.reversed_by_user_id, settlement.reversed_at = actor, ctx.now
    await _bump(ctx, settlement)
    await ledger.finish()
    await _record(ctx, settlement, "finance.settlement_reversed")
    return await settlement_view(ctx, settlement)


# --- helpers -------------------------------------------------------------------------


def _own_participant(ledger: Ledger) -> UUID | None:
    participant = ledger.access.participant
    return participant.id if participant is not None else None


def _manages_settlements(ledger: Ledger) -> bool:
    decision = decide_plan(PlanAction.MANAGE_SETTLEMENTS, ledger.access.subject)
    return decision is Decision.ALLOW


def _require_creditor(ledger: Ledger, creditor: PlanParticipant, message: str) -> None:
    if _own_participant(ledger) == creditor.id:
        return
    if creditor.identity_kind == "placeholder" and _manages_settlements(ledger):
        return
    raise forbidden(message)


def _move_dispute_count(
    ledger: Ledger, current: SettlementStatus, target: SettlementStatus
) -> bool:
    """Keep the head's open-dispute count in step; True when it changed."""

    changed = False
    if current is SettlementStatus.DISPUTED:
        ledger.head.disputed_settlements -= 1
        changed = True
    if target is SettlementStatus.DISPUTED:
        ledger.head.disputed_settlements += 1
        changed = True
    return changed


def _new_settlement(
    ctx: CommandContext,
    plan_id: UUID,
    settlement_id: UUID | None,
    *,
    kind: str,
    debtor: PlanParticipant,
    creditor: PlanParticipant,
    currency: str,
    amount_minor: int,
    confirmed: bool,
    note: str | None,
    occurred_on: date,
) -> Settlement:
    actor = ctx.require_actor().user_id
    return Settlement(
        id=settlement_id or new_id(),
        plan_id=plan_id,
        kind=kind,
        from_participant_id=debtor.id,
        to_participant_id=creditor.id,
        currency=currency,
        amount_minor=amount_minor,
        paid_currency=None,
        paid_amount_minor=None,
        fx_snapshot_id=None,
        method=None,
        fee_minor=None,
        note=note,
        occurred_on=occurred_on,
        status=(SettlementStatus.CONFIRMED if confirmed else SettlementStatus.RECORDED).value,
        overpaid=False,
        recorded_by_user_id=actor,
        confirmed_by_user_id=actor if confirmed else None,
        confirmed_at=ctx.now if confirmed else None,
        disputed_by_user_id=None,
        disputed_at=None,
        reversed_by_user_id=None,
        reversed_at=None,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )


async def _insert(ctx: CommandContext, settlement: Settlement) -> None:
    try:
        async with ctx.savepoint():
            ctx.session.add(settlement)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error


async def _bump(ctx: CommandContext, settlement: Settlement) -> None:
    settlement.version += 1
    settlement.updated_at = ctx.now
    await ctx.session.flush()


async def _find(
    ctx: CommandContext, plan_id: UUID, settlement_id: UUID, *, for_update: bool = False
) -> Settlement | None:
    statement = select(Settlement).where(
        Settlement.plan_id == plan_id, Settlement.id == settlement_id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def _locked(ctx: CommandContext, plan_id: UUID, settlement_id: UUID) -> Settlement:
    settlement = await _find(ctx, plan_id, settlement_id, for_update=True)
    if settlement is None:
        raise not_found()
    return settlement


async def _record(ctx: CommandContext, settlement: Settlement, action: str) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=SETTLEMENT_ENTITY,
        entity_id=settlement.id,
        entity_version=settlement.version,
        scope=ChangeScope.PLAN,
        scope_id=settlement.plan_id,
        plan_id=settlement.plan_id,
        metadata={"version": settlement.version, "status": settlement.status},
    )
