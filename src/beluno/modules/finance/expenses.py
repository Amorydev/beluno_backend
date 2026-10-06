"""Expenses: immutable revisions, exact reversals, voids, and refunds.

Creating an expense appends revision 1 and its ``expense`` transaction. Revising
appends the exact reversal of the current revision's transaction and then a new
revision with full postings; voiding appends only reversals (of the revision
and of every refund still in effect). Refunds belong to the expense aggregate:
each one bumps the expense version and appends a ``refund`` transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import Decision, PlanAction, decide_plan
from beluno.contracts.errors import conflict, forbidden, invalid_state, not_found, version_conflict
from beluno.db.ids import new_id
from beluno.db.models.finance import (
    Expense,
    ExpensePayer,
    ExpenseRefund,
    ExpenseRevision,
    ExpenseSplit,
    FxSnapshot,
    LedgerTransaction,
    RefundShare,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.errors import refund_exceeds_amount, split_invalid
from beluno.modules.finance.fx import RateSource, convert, parse_rate
from beluno.modules.finance.ledger import Ledger, open_ledger
from beluno.modules.finance.money import check_amount
from beluno.modules.finance.postings import (
    Party,
    expense_postings,
    refund_allocation,
    refund_postings,
)
from beluno.modules.finance.splits import (
    SPLIT_ALGORITHM,
    Payer,
    Share,
    SplitSpec,
    resolve_split,
    validate_payers,
)
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

EXPENSE_ENTITY = "expense"


@dataclass(frozen=True)
class RateInput:
    rate: str
    source: RateSource
    as_of: datetime | None


@dataclass(frozen=True)
class ExpenseDraft:
    description: str
    category: str
    occurred_on: date
    notes: str | None
    amount_minor: int
    currency: str
    payers: tuple[Payer, ...]
    split: SplitSpec
    split_input: dict[str, Any]
    base_rate: RateInput | None = None


@dataclass(frozen=True)
class RefundDraft:
    refund_id: UUID | None
    amount_minor: int
    recipient: Party
    shares: tuple[Share, ...] | None
    note: str | None


@dataclass(frozen=True)
class RefundView:
    refund: ExpenseRefund
    shares: list[RefundShare]
    reversed: bool


@dataclass(frozen=True)
class RevisionView:
    revision: ExpenseRevision
    payers: list[ExpensePayer]
    splits: list[ExpenseSplit]
    base_rate: FxSnapshot | None


@dataclass(frozen=True)
class ExpenseView:
    expense: Expense
    current: RevisionView
    refunds: list[RefundView]

    @property
    def refunded_minor(self) -> int:
        return sum(view.refund.amount_minor for view in self.refunds if not view.reversed)


# --- reads ---------------------------------------------------------------------------


async def get_expense(ctx: CommandContext, plan_id: UUID, expense_id: UUID) -> ExpenseView:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    expense = await _find(ctx, plan_id, expense_id)
    if expense is None:
        raise not_found()
    return await expense_view(ctx, expense)


async def list_expenses(
    ctx: CommandContext, plan_id: UUID, *, after: UUID | None, limit: int
) -> list[ExpenseView]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    statement = select(Expense).where(Expense.plan_id == plan_id)
    if after is not None:
        statement = statement.where(Expense.id > after)
    rows = await ctx.session.execute(statement.order_by(Expense.id).limit(limit))
    return [await expense_view(ctx, expense) for expense in rows.scalars()]


async def list_revisions(
    ctx: CommandContext, plan_id: UUID, expense_id: UUID
) -> list[RevisionView]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    if await _find(ctx, plan_id, expense_id) is None:
        raise not_found()
    rows = await ctx.session.execute(
        select(ExpenseRevision)
        .where(ExpenseRevision.expense_id == expense_id)
        .order_by(ExpenseRevision.revision_number)
    )
    return [await _revision_view(ctx, revision) for revision in rows.scalars()]


async def expense_view(ctx: CommandContext, expense: Expense) -> ExpenseView:
    revision = await ctx.session.get(ExpenseRevision, expense.current_revision_id)
    assert revision is not None
    refunds = (
        await ctx.session.execute(
            select(ExpenseRefund)
            .where(ExpenseRefund.expense_id == expense.id)
            .order_by(ExpenseRefund.created_at, ExpenseRefund.id)
        )
    ).scalars()
    return ExpenseView(
        expense=expense,
        current=await _revision_view(ctx, revision),
        refunds=[await _refund_view(ctx, refund) for refund in refunds],
    )


async def _revision_view(ctx: CommandContext, revision: ExpenseRevision) -> RevisionView:
    payers = await ctx.session.execute(
        select(ExpensePayer)
        .where(ExpensePayer.revision_id == revision.id)
        .order_by(ExpensePayer.position)
    )
    splits = await ctx.session.execute(
        select(ExpenseSplit)
        .where(ExpenseSplit.revision_id == revision.id)
        .order_by(ExpenseSplit.position)
    )
    rate = (
        await ctx.session.get(FxSnapshot, revision.base_fx_snapshot_id)
        if revision.base_fx_snapshot_id
        else None
    )
    return RevisionView(
        revision=revision,
        payers=list(payers.scalars()),
        splits=list(splits.scalars()),
        base_rate=rate,
    )


async def _refund_view(ctx: CommandContext, refund: ExpenseRefund) -> RefundView:
    shares = await ctx.session.execute(
        select(RefundShare)
        .where(RefundShare.refund_id == refund.id)
        .order_by(RefundShare.participant_id)
    )
    return RefundView(
        refund=refund,
        shares=list(shares.scalars()),
        reversed=await _refund_reversed(ctx, refund.id),
    )


async def _refund_reversed(ctx: CommandContext, refund_id: UUID) -> bool:
    transaction = await _transaction(ctx, refund_id=refund_id)
    reversal = await ctx.session.execute(
        select(LedgerTransaction.id).where(
            LedgerTransaction.reverses_transaction_id == transaction.id
        )
    )
    return reversal.scalar_one_or_none() is not None


# --- writes --------------------------------------------------------------------------


async def create_expense(
    ctx: CommandContext, plan_id: UUID, expense_id: UUID | None, draft: ExpenseDraft
) -> ExpenseView:
    ledger = await open_ledger(ctx, plan_id, PlanAction.CREATE_EXPENSE)
    actor = ctx.require_actor()
    expense_id = expense_id or new_id()
    revision_id = new_id()
    expense = Expense(
        id=expense_id,
        plan_id=plan_id,
        state="active",
        current_revision_id=revision_id,
        created_by_user_id=actor.user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
        voided_at=None,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(expense)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _append_revision(ledger, expense, revision_id, 1, draft, keep=())
    await ledger.finish()
    await _record(ctx, expense, "finance.expense_created")
    return await expense_view(ctx, expense)


async def revise_expense(
    ctx: CommandContext,
    plan_id: UUID,
    expense_id: UUID,
    draft: ExpenseDraft,
    expected_version: int,
) -> ExpenseView:
    ledger, expense = await _open_expense(ctx, plan_id, expense_id, expected_version)
    current = await _revision_view(ctx, await _current_revision(ctx, expense))
    refunds = await _live_refunds(ctx, expense.id)
    if refunds:
        if draft.currency != current.revision.currency:
            raise invalid_state("A refunded expense keeps its currency; void it and add a new one")
        if draft.amount_minor < sum(refund.amount_minor for refund in refunds):
            raise refund_exceeds_amount()
    previous = {payer.participant_id for payer in current.payers if payer.participant_id}
    previous |= {split.participant_id for split in current.splits}
    await ledger.reverse(
        await _transaction(ctx, revision_id=current.revision.id), kind="expense_reversal"
    )
    revision_id = new_id()
    await _append_revision(
        ledger,
        expense,
        revision_id,
        current.revision.revision_number + 1,
        draft,
        keep=previous,
    )
    expense.current_revision_id = revision_id
    expense.version += 1
    expense.updated_at = ctx.now
    await ctx.session.flush()
    await ledger.finish()
    await _record(ctx, expense, "finance.expense_revised")
    return await expense_view(ctx, expense)


async def void_expense(
    ctx: CommandContext, plan_id: UUID, expense_id: UUID, expected_version: int
) -> ExpenseView:
    ledger, expense = await _open_expense(ctx, plan_id, expense_id, expected_version)
    await ledger.reverse(
        await _transaction(ctx, revision_id=expense.current_revision_id), kind="expense_reversal"
    )
    for refund in await _live_refunds(ctx, expense.id):
        await ledger.reverse(await _transaction(ctx, refund_id=refund.id), kind="expense_reversal")
    expense.state = "voided"
    expense.voided_at = ctx.now
    expense.version += 1
    expense.updated_at = ctx.now
    await ctx.session.flush()
    await ledger.finish()
    await _record(ctx, expense, "finance.expense_voided")
    return await expense_view(ctx, expense)


async def refund_expense(
    ctx: CommandContext,
    plan_id: UUID,
    expense_id: UUID,
    draft: RefundDraft,
    expected_version: int,
) -> ExpenseView:
    ledger, expense = await _open_expense(ctx, plan_id, expense_id, expected_version)
    current = await _revision_view(ctx, await _current_revision(ctx, expense))
    revision = current.revision
    check_amount(draft.amount_minor, field="amount_minor")
    refunded = sum(refund.amount_minor for refund in await _live_refunds(ctx, expense.id))
    if refunded + draft.amount_minor > revision.amount_minor:
        raise refund_exceeds_amount()
    in_revision = {split.participant_id for split in current.splits}
    in_revision |= {payer.participant_id for payer in current.payers if payer.participant_id}
    recipient = draft.recipient
    if recipient.participant_id is not None:
        ledger.participant(recipient.participant_id, keep=in_revision)
    if draft.shares is None:
        owed = [Share(split.participant_id, split.owed_minor) for split in current.splits]
        shares = _merge_shares(ledger, refund_allocation(draft.amount_minor, owed))
    else:
        for share in draft.shares:
            ledger.participant(share.participant_id, keep=in_revision)
            check_amount(share.owed_minor, field="share amount_minor")
        if len({share.participant_id for share in draft.shares}) != len(draft.shares):
            raise split_invalid("each participant may appear only once")
        if sum(share.owed_minor for share in draft.shares) != draft.amount_minor:
            raise split_invalid("refund shares must add up to the refund amount")
        shares = list(draft.shares)
    refund = ExpenseRefund(
        id=draft.refund_id or new_id(),
        plan_id=plan_id,
        expense_id=expense.id,
        amount_minor=draft.amount_minor,
        currency=revision.currency,
        recipient_participant_id=recipient.participant_id,
        note=draft.note,
        created_by_user_id=ctx.require_actor().user_id,
        created_at=ctx.now,
    )
    try:
        async with ctx.savepoint():
            ctx.session.add(refund)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    for share in shares:
        ctx.session.add(
            RefundShare(
                refund_id=refund.id,
                participant_id=share.participant_id,
                plan_id=plan_id,
                amount_minor=share.owed_minor,
            )
        )
    await ctx.session.flush()
    await ledger.append(
        kind="refund",
        postings={revision.currency: refund_postings(recipient, shares)},
        expense_id=expense.id,
        refund_id=refund.id,
    )
    expense.version += 1
    expense.updated_at = ctx.now
    await ctx.session.flush()
    await ledger.finish()
    await _record(ctx, expense, "finance.expense_refunded")
    return await expense_view(ctx, expense)


# --- helpers -------------------------------------------------------------------------


async def _open_expense(
    ctx: CommandContext, plan_id: UUID, expense_id: UUID, expected_version: int
) -> tuple[Ledger, Expense]:
    """Lock the ledger and the expense; only its creator or a manager may change it."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.CREATE_EXPENSE)
    expense = await _find(ctx, plan_id, expense_id, for_update=True)
    if expense is None:
        raise not_found()
    actor = ctx.require_actor()
    may_manage = decide_plan(PlanAction.MANAGE_EXPENSES, ledger.access.subject) is Decision.ALLOW
    if expense.created_by_user_id != actor.user_id and not may_manage:
        raise forbidden("Only the person who added this expense or a plan manager can change it")
    if expense.version != expected_version:
        raise version_conflict(expense)
    if expense.state == "voided":
        raise invalid_state("A voided expense cannot change; add a new expense instead")
    return ledger, expense


async def _append_revision(
    ledger: Ledger,
    expense: Expense,
    revision_id: UUID,
    number: int,
    draft: ExpenseDraft,
    *,
    keep: Iterable[UUID],
) -> None:
    ctx = ledger.ctx
    kept = set(keep)
    check_amount(draft.amount_minor, field="amount_minor")
    currency = await ledger.currency(draft.currency)
    payers = validate_payers(draft.amount_minor, draft.payers)
    shares = resolve_split(draft.amount_minor, draft.split)
    for payer in payers:
        if payer.participant_id is not None:
            ledger.participant(payer.participant_id, keep=kept)
    for share in shares:
        ledger.participant(share.participant_id, keep=kept)
    base_currency = ledger.access.plan.base_currency
    base_amount, snapshot = await _base_amount(ledger, draft, currency.exponent, base_currency)
    revision = ExpenseRevision(
        id=revision_id,
        plan_id=expense.plan_id,
        expense_id=expense.id,
        revision_number=number,
        amount_minor=draft.amount_minor,
        currency=draft.currency,
        description=draft.description,
        category=draft.category,
        occurred_on=draft.occurred_on,
        notes=draft.notes,
        split_method=draft.split.method.value,
        split_algorithm=SPLIT_ALGORITHM,
        split_input=draft.split_input,
        base_currency=base_currency,
        base_amount_minor=base_amount,
        base_fx_snapshot_id=snapshot.id if snapshot else None,
        commitment_id=None,
        created_by_user_id=ctx.require_actor().user_id,
        created_at=ctx.now,
    )
    ctx.session.add(revision)
    await ctx.session.flush()
    for position, payer in enumerate(payers):
        ctx.session.add(
            ExpensePayer(
                revision_id=revision_id,
                position=position,
                plan_id=expense.plan_id,
                participant_id=payer.participant_id,
                amount_minor=payer.amount_minor,
            )
        )
    for position, share in enumerate(shares):
        ctx.session.add(
            ExpenseSplit(
                revision_id=revision_id,
                position=position,
                plan_id=expense.plan_id,
                participant_id=share.participant_id,
                owed_minor=share.owed_minor,
            )
        )
    await ctx.session.flush()
    await ledger.append(
        kind="expense",
        postings={draft.currency: expense_postings(payers, shares)},
        expense_id=expense.id,
        revision_id=revision_id,
    )


async def _base_amount(
    ledger: Ledger, draft: ExpenseDraft, exponent: int, base_currency: str
) -> tuple[int | None, FxSnapshot | None]:
    """The labelled base-currency value: identity, a stored snapshot, or unknown."""

    if draft.currency == base_currency:
        return draft.amount_minor, None
    if draft.base_rate is None:
        return None, None
    base = await ledger.currency(base_currency)
    rate = parse_rate(draft.base_rate.rate)
    snapshot = await record_rate(
        ledger,
        base=draft.currency,
        quote=base_currency,
        rate=rate,
        source=draft.base_rate.source,
        as_of=draft.base_rate.as_of,
    )
    amount = convert(
        draft.amount_minor, from_exponent=exponent, to_exponent=base.exponent, rate=rate
    )
    return amount, snapshot


async def record_rate(
    ledger: Ledger,
    *,
    base: str,
    quote: str,
    rate: Decimal,
    source: RateSource,
    as_of: datetime | None,
) -> FxSnapshot:
    ctx = ledger.ctx
    snapshot = FxSnapshot(
        id=new_id(),
        plan_id=ledger.plan_id,
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        source=source.value,
        rounding_mode="half_even",
        as_of=as_of or ctx.now,
        created_by_user_id=ctx.require_actor().user_id,
        created_at=ctx.now,
    )
    ctx.session.add(snapshot)
    await ctx.session.flush()
    return snapshot


def _merge_shares(ledger: Ledger, shares: Sequence[Share]) -> list[Share]:
    """Move shares of merged participants to the survivor, keeping first-seen order."""

    merged: dict[UUID, int] = {}
    for share in shares:
        holder = ledger.resolve(share.participant_id)
        merged[holder] = merged.get(holder, 0) + share.owed_minor
    return [Share(participant_id, amount) for participant_id, amount in merged.items()]


async def _find(
    ctx: CommandContext, plan_id: UUID, expense_id: UUID, *, for_update: bool = False
) -> Expense | None:
    statement = select(Expense).where(Expense.plan_id == plan_id, Expense.id == expense_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def _current_revision(ctx: CommandContext, expense: Expense) -> ExpenseRevision:
    revision = await ctx.session.get(ExpenseRevision, expense.current_revision_id)
    assert revision is not None
    return revision


async def _transaction(
    ctx: CommandContext, *, revision_id: UUID | None = None, refund_id: UUID | None = None
) -> LedgerTransaction:
    statement = select(LedgerTransaction)
    if revision_id is not None:
        statement = statement.where(
            LedgerTransaction.revision_id == revision_id, LedgerTransaction.kind == "expense"
        )
    else:
        statement = statement.where(
            LedgerTransaction.refund_id == refund_id, LedgerTransaction.kind == "refund"
        )
    return (await ctx.session.execute(statement)).scalar_one()


async def _live_refunds(ctx: CommandContext, expense_id: UUID) -> list[ExpenseRefund]:
    refunds = (
        await ctx.session.execute(
            select(ExpenseRefund).where(ExpenseRefund.expense_id == expense_id)
        )
    ).scalars()
    return [refund for refund in refunds if not await _refund_reversed(ctx, refund.id)]


async def _record(ctx: CommandContext, expense: Expense, action: str) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=EXPENSE_ENTITY,
        entity_id=expense.id,
        entity_version=expense.version,
        scope=ChangeScope.PLAN,
        scope_id=expense.plan_id,
        plan_id=expense.plan_id,
        metadata={"version": expense.version},
    )
