"""The plan's virtual fund and privileged ledger adjustments.

The fund is a ledger account per currency, not money Beluno holds: participants
record that they pooled cash with a custodian, and the app only keeps the
accounting. A contribution posts the contributor ``+x`` and the fund ``-x``; a
withdrawal is the opposite; a fund-paid expense names the fund as payer. The
money available is minus the fund balance and can never go below zero.
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
    precondition_required,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.finance import FundMovement, FundSettings, LedgerTransaction
from beluno.modules.context import CommandContext
from beluno.modules.finance.errors import entry_unbalanced, split_invalid
from beluno.modules.finance.ledger import LEDGER_ENTITY, Ledger, open_ledger
from beluno.modules.finance.money import MAX_AMOUNT_MINOR, check_amount
from beluno.modules.finance.postings import FUND, Party, adjustment_postings, transfer_postings
from beluno.modules.sync_audit.recorder import ChangeScope, record_audit, record_mutation

FUND_ENTITY = "fund"
MOVEMENT_ENTITY = "fund_movement"
CONTRIBUTION = "contribution"
WITHDRAWAL = "withdrawal"


@dataclass(frozen=True)
class MovementDraft:
    movement_id: UUID | None
    participant_id: UUID
    currency: str
    amount_minor: int
    note: str | None
    occurred_on: date


@dataclass(frozen=True)
class AdjustmentDraft:
    currency: str
    memo: str
    entries: tuple[tuple[Party, int], ...]


# --- reads ---------------------------------------------------------------------------


async def get_settings(ctx: CommandContext, plan_id: UUID) -> FundSettings | None:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    return await ctx.session.get(FundSettings, plan_id)


async def list_movements(
    ctx: CommandContext, plan_id: UUID, *, after: UUID | None, limit: int
) -> list[FundMovement]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    statement = select(FundMovement).where(FundMovement.plan_id == plan_id)
    if after is not None:
        statement = statement.where(FundMovement.id > after)
    rows = await ctx.session.execute(statement.order_by(FundMovement.id).limit(limit))
    return list(rows.scalars())


# --- writes --------------------------------------------------------------------------


async def put_settings(
    ctx: CommandContext,
    plan_id: UUID,
    *,
    custodian_participant_id: UUID | None,
    note: str | None,
    expected_version: int | None,
) -> FundSettings:
    """Create the fund settings (no version) or replace them (current version required)."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.MANAGE_FUND)
    ledger.require_trip()
    if custodian_participant_id is not None:
        ledger.participant(custodian_participant_id)
    settings = (
        await ctx.session.execute(
            select(FundSettings)
            .where(FundSettings.plan_id == plan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if settings is None:
        settings = FundSettings(
            plan_id=plan_id,
            custodian_participant_id=custodian_participant_id,
            note=note,
            version=1,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        ctx.session.add(settings)
    else:
        if expected_version is None:
            raise precondition_required()
        if settings.version != expected_version:
            raise version_conflict(settings)
        settings.custodian_participant_id = custodian_participant_id
        settings.note = note
        settings.version += 1
        settings.updated_at = ctx.now
    await ctx.session.flush()
    await _record(ctx, plan_id, FUND_ENTITY, plan_id, settings.version, "finance.fund_updated")
    return settings


async def contribute(ctx: CommandContext, plan_id: UUID, draft: MovementDraft) -> FundMovement:
    ledger = await open_ledger(ctx, plan_id, PlanAction.CONTRIBUTE_FUND)
    ledger.require_trip()
    contributor = ledger.participant(draft.participant_id)
    own = ledger.access.participant
    manages = decide_plan(PlanAction.MANAGE_FUND, ledger.access.subject) is Decision.ALLOW
    if (own is None or own.id != contributor.id) and not manages:
        raise forbidden("Record your own contributions, or ask a plan manager")
    movement = await _movement(ledger, CONTRIBUTION, draft)
    await ledger.append(
        kind="fund_contribution",
        postings={
            draft.currency: transfer_postings(Party(contributor.id), FUND, draft.amount_minor)
        },
        fund_movement_id=movement.id,
    )
    await ledger.finish()
    await _record_movement(ctx, movement, "finance.fund_contributed")
    return movement


async def withdraw(ctx: CommandContext, plan_id: UUID, draft: MovementDraft) -> FundMovement:
    """Money handed back from the fund to a participant (managers only)."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.MANAGE_FUND)
    ledger.require_trip()
    receiver = ledger.settling_party(draft.participant_id)
    movement = await _movement(ledger, WITHDRAWAL, draft)
    await ledger.append(
        kind="fund_withdrawal",
        postings={draft.currency: transfer_postings(FUND, Party(receiver.id), draft.amount_minor)},
        fund_movement_id=movement.id,
    )
    await ledger.finish()
    await _record_movement(ctx, movement, "finance.fund_withdrawn")
    return movement


async def adjust_ledger(
    ctx: CommandContext, plan_id: UUID, draft: AdjustmentDraft
) -> LedgerTransaction:
    """A privileged, audited correction between accounts of one currency (owner, step-up)."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.ADJUST_LEDGER)
    await ledger.currency(draft.currency)
    if not 2 <= len(draft.entries) <= 100:
        raise split_invalid("an adjustment moves money between 2 to 100 accounts")
    parties = [party for party, _ in draft.entries]
    if len(set(parties)) != len(parties):
        raise split_invalid("each account may appear only once")
    for party, amount in draft.entries:
        if party.participant_id is not None:
            ledger.settling_party(party.participant_id)
        if amount == 0 or abs(amount) > MAX_AMOUNT_MINOR:
            raise split_invalid(f"amounts must be non-zero and at most {MAX_AMOUNT_MINOR}")
    if sum(amount for _, amount in draft.entries) != 0:
        raise entry_unbalanced()
    subtype = "fund_adjustment" if FUND in parties else "correction"
    transaction = await ledger.append(
        kind="adjustment",
        subtype=subtype,
        memo=draft.memo,
        postings={draft.currency: adjustment_postings(draft.entries)},
    )
    await ledger.finish()
    # ``finish`` already published the ledger change; this only audits who adjusted.
    await record_audit(
        ctx,
        action="finance.ledger_adjusted",
        entity_type=LEDGER_ENTITY,
        entity_id=plan_id,
        plan_id=plan_id,
        metadata={"ledger_seq": transaction.ledger_seq, "subtype": subtype},
    )
    return transaction


# --- helpers -------------------------------------------------------------------------


async def _movement(ledger: Ledger, kind: str, draft: MovementDraft) -> FundMovement:
    ctx = ledger.ctx
    check_amount(draft.amount_minor, field="amount_minor")
    await ledger.currency(draft.currency)
    movement = FundMovement(
        id=draft.movement_id or new_id(),
        plan_id=ledger.plan_id,
        kind=kind,
        participant_id=draft.participant_id,
        currency=draft.currency,
        amount_minor=draft.amount_minor,
        note=draft.note,
        occurred_on=draft.occurred_on,
        created_by_user_id=ctx.require_actor().user_id,
        created_at=ctx.now,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(movement)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    return movement


async def _record_movement(ctx: CommandContext, movement: FundMovement, action: str) -> None:
    await _record(ctx, movement.plan_id, MOVEMENT_ENTITY, movement.id, 1, action)


async def _record(
    ctx: CommandContext,
    plan_id: UUID,
    entity_type: str,
    entity_id: UUID,
    version: int,
    action: str,
    *,
    metadata: dict[str, object] | None = None,
) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=version,
        scope=ChangeScope.PLAN,
        scope_id=plan_id,
        plan_id=plan_id,
        metadata=metadata or {"version": version},
    )
