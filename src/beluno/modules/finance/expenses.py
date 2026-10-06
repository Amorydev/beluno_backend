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
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

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
    FundSettings,
    FxSnapshot,
    LedgerTransaction,
    RefundShare,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.commitments import link_expense, release_expense
from beluno.modules.finance.errors import refund_exceeds_amount, split_invalid
from beluno.modules.finance.fx import convert, parse_rate
from beluno.modules.finance.ledger import Ledger, open_ledger
from beluno.modules.finance.money import check_amount
from beluno.modules.finance.postings import (
    Party,
    expense_postings,
    refund_allocation,
    refund_postings,
)
from beluno.modules.finance.rates import RateInput, record_rate
from beluno.modules.finance.splits import (
    SPLIT_ALGORITHM,
    Payer,
    Share,
    SplitSpec,
    resolve_split,
    validate_payers,
)
from beluno.modules.iam.users import is_actor_account
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

EXPENSE_ENTITY = "expense"


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
    # The cost commitment this expense now accounts for (converted atomically).
    commitment_id: UUID | None = None


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
    return await expense_views(ctx, list(rows.scalars()))


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
    return await _revision_views(ctx, list(rows.scalars()))


async def expense_view(ctx: CommandContext, expense: Expense) -> ExpenseView:
    return (await expense_views(ctx, [expense]))[0]


async def expense_views(ctx: CommandContext, expenses: Sequence[Expense]) -> list[ExpenseView]:
    """Render many expenses with a fixed number of queries."""

    if not expenses:
        return []
    revisions = {
        row.id: row
        for row in (
            await ctx.session.execute(
                select(ExpenseRevision).where(
                    ExpenseRevision.id.in_([e.current_revision_id for e in expenses])
                )
            )
        ).scalars()
    }
    current = {
        view.revision.id: view for view in await _revision_views(ctx, list(revisions.values()))
    }
    refunds: dict[UUID, list[RefundView]] = {expense.id: [] for expense in expenses}
    for view in await _refund_views(ctx, [expense.id for expense in expenses]):
        refunds[view.refund.expense_id].append(view)
    return [
        ExpenseView(
            expense=expense,
            current=current[expense.current_revision_id],
            refunds=refunds[expense.id],
        )
        for expense in expenses
    ]


async def _revision_views(
    ctx: CommandContext, revisions: Sequence[ExpenseRevision]
) -> list[RevisionView]:
    if not revisions:
        return []
    ids = [revision.id for revision in revisions]
    payers: dict[UUID, list[ExpensePayer]] = {revision_id: [] for revision_id in ids}
    for payer in (
        await ctx.session.execute(
            select(ExpensePayer)
            .where(ExpensePayer.revision_id.in_(ids))
            .order_by(ExpensePayer.revision_id, ExpensePayer.position)
        )
    ).scalars():
        payers[payer.revision_id].append(payer)
    splits: dict[UUID, list[ExpenseSplit]] = {revision_id: [] for revision_id in ids}
    for split in (
        await ctx.session.execute(
            select(ExpenseSplit)
            .where(ExpenseSplit.revision_id.in_(ids))
            .order_by(ExpenseSplit.revision_id, ExpenseSplit.position)
        )
    ).scalars():
        splits[split.revision_id].append(split)
    snapshot_ids = [r.base_fx_snapshot_id for r in revisions if r.base_fx_snapshot_id]
    snapshots: dict[UUID, FxSnapshot] = {}
    if snapshot_ids:
        snapshots = {
            row.id: row
            for row in (
                await ctx.session.execute(select(FxSnapshot).where(FxSnapshot.id.in_(snapshot_ids)))
            ).scalars()
        }
    return [
        RevisionView(
            revision=revision,
            payers=payers[revision.id],
            splits=splits[revision.id],
            base_rate=snapshots.get(revision.base_fx_snapshot_id)
            if revision.base_fx_snapshot_id
            else None,
        )
        for revision in revisions
    ]


async def _refund_views(ctx: CommandContext, expense_ids: Sequence[UUID]) -> list[RefundView]:
    refunds = list(
        (
            await ctx.session.execute(
                select(ExpenseRefund)
                .where(ExpenseRefund.expense_id.in_(expense_ids))
                .order_by(ExpenseRefund.created_at, ExpenseRefund.id)
            )
        ).scalars()
    )
    if not refunds:
        return []
    refund_ids = [refund.id for refund in refunds]
    shares: dict[UUID, list[RefundShare]] = {refund_id: [] for refund_id in refund_ids}
    for share in (
        await ctx.session.execute(
            select(RefundShare)
            .where(RefundShare.refund_id.in_(refund_ids))
            .order_by(RefundShare.refund_id, RefundShare.participant_id)
        )
    ).scalars():
        shares[share.refund_id].append(share)
    reversal = aliased(LedgerTransaction)
    reversed_ids = set(
        (
            await ctx.session.execute(
                select(LedgerTransaction.refund_id)
                .join(reversal, reversal.reverses_transaction_id == LedgerTransaction.id)
                .where(LedgerTransaction.refund_id.in_(refund_ids))
            )
        ).scalars()
    )
    return [
        RefundView(refund=refund, shares=shares[refund.id], reversed=refund.id in reversed_ids)
        for refund in refunds
    ]


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
    if draft.commitment_id is not None:
        await link_expense(ledger, draft.commitment_id, expense)
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
    current = (await _revision_views(ctx, [await _current_revision(ctx, expense)]))[0]
    refunds = await _live_refunds(ctx, expense.id)
    if refunds:
        if draft.currency != current.revision.currency:
            raise invalid_state("A refunded expense keeps its currency; void it and add a new one")
        if draft.amount_minor < sum(refund.amount_minor for refund in refunds):
            raise refund_exceeds_amount()
        # Refunds credit the people who bore the cost under this split; a new split
        # would leave that credit with people who no longer share the expense.
        if draft.split_input != current.revision.split_input:
            raise invalid_state("A refunded expense keeps its split; void it and add a new one")
    previous = {payer.participant_id for payer in current.payers if payer.participant_id}
    previous |= {split.participant_id for split in current.splits}
    previous_link = current.revision.commitment_id
    if previous_link is not None and previous_link != draft.commitment_id:
        await release_expense(ledger, previous_link, expense.id)
    if draft.commitment_id is not None:
        await link_expense(ledger, draft.commitment_id, expense)
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
    linked = (await _current_revision(ctx, expense)).commitment_id
    if linked is not None:
        await release_expense(ledger, linked, expense.id)
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
    current = (await _revision_views(ctx, [await _current_revision(ctx, expense)]))[0]
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
    may_manage = decide_plan(PlanAction.MANAGE_EXPENSES, ledger.access.subject) is Decision.ALLOW
    if not may_manage and not await is_actor_account(ctx, expense.created_by_user_id):
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
        else:
            await _require_fund_spender(ledger)
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
        commitment_id=draft.commitment_id,
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


async def _require_fund_spender(ledger: Ledger) -> None:
    """Spending pooled money is a fund manager's or the custodian's call, like a withdrawal."""

    ledger.require_trip()
    if decide_plan(PlanAction.MANAGE_FUND, ledger.access.subject) is Decision.ALLOW:
        return
    settings = await ledger.ctx.session.get(FundSettings, ledger.plan_id)
    own = ledger.access.participant
    if settings is not None and own is not None and settings.custodian_participant_id == own.id:
        return
    raise forbidden("Only the fund's custodian or a plan manager can pay from the fund")


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
