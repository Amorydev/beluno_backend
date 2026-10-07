"""A plan's money line by line: the accounting CSV's rows and the report's spending.

``journal_lines`` reads the ledger itself: one row per account (a person, or the
kitty) and journal entry, holding how much that entry moved the balance. Every kind
of entry is there (expenses and their changes, refunds, payments and forgiven debts,
the kitty, consolidations, corrections, merges), so per person and currency the rows
add up to the ledger balance by construction. Owner memos are left out.

``spending_lines`` takes what each person paid and owes for expenses (refunds
netted) from the sync snapshot, for the report's people table.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from beluno.db.models.finance import ExpenseRevision, FundMovement, Settlement
from beluno.modules.context import CommandContext
from beluno.modules.finance.ledger import MAX_MERGE_DEPTH
from beluno.modules.finance.views import TransactionView, list_transactions

KITTY: str | None = None  # the kitty's rows carry no participant
PAGE = 500


@dataclass(frozen=True)
class JournalLine:
    occurred_on: str
    entry: str  # the entry's kind, or its subtype (waiver, correction, merge_transfer, ...)
    description: str
    category: str
    participant_id: str | None  # None: the kitty
    currency: str
    amount_minor: int  # what the entry moved this balance by
    ledger_seq: int
    reference: str  # the expense, settlement, kitty movement, or consolidation id


async def journal_lines(ctx: CommandContext, plan_id: UUID, zone: str | None) -> list[JournalLine]:
    """Every posting of the plan's journal, in ledger order (checks finance access)."""

    views: list[TransactionView] = []
    after: int | None = None
    while True:
        page = await list_transactions(ctx, plan_id, after_seq=after, limit=PAGE)
        views.extend(page)
        if len(page) < PAGE:
            break
        after = page[-1].transaction.ledger_seq
    revisions = await _revisions(ctx, {v.transaction.revision_id for v in views})
    settled_on = await _dates(ctx, Settlement, {v.transaction.settlement_id for v in views})
    moved_on = await _dates(ctx, FundMovement, {v.transaction.fund_movement_id for v in views})
    local = ZoneInfo(zone or "UTC")
    lines: list[JournalLine] = []
    for view in views:
        tx = view.transaction
        description, category, day = "", "", _local_day(tx.created_at, local)
        if tx.revision_id in revisions:
            description, category, day = revisions[tx.revision_id]
        elif tx.settlement_id in settled_on:
            day = settled_on[tx.settlement_id]
        elif tx.fund_movement_id in moved_on:
            day = moved_on[tx.fund_movement_id]
        reference = next(
            (
                str(value)
                for value in (
                    tx.expense_id,
                    tx.settlement_id,
                    tx.fund_movement_id,
                    tx.consolidation_id,
                )
                if value is not None
            ),
            str(tx.id),
        )
        totals: dict[tuple[UUID | None, str], int] = defaultdict(int)
        for posting in view.postings:
            totals[(posting.participant_id, posting.currency)] += posting.amount_minor
        lines.extend(
            JournalLine(
                occurred_on=day.isoformat(),
                entry=tx.subtype or tx.kind,
                description=description,
                category=category,
                participant_id=str(participant_id) if participant_id else KITTY,
                currency=currency,
                amount_minor=amount,
                ledger_seq=tx.ledger_seq,
                reference=reference,
            )
            for (participant_id, currency), amount in totals.items()
            if amount
        )
    return lines


@dataclass(frozen=True)
class SpendingLine:
    participant_id: str | None  # None: the kitty
    currency: str
    paid_minor: int
    owed_minor: int


def spending_lines(entities: dict[str, list[dict[str, Any]]]) -> list[SpendingLine]:
    """What each person paid and owes for active expenses, refunds netted."""

    merged = {
        row["id"]: row["merged_into_participant_id"]
        for row in entities.get("plan_participant", [])
        if row.get("merged_into_participant_id")
    }

    def holder(participant_id: str | None) -> str | None:
        # A merged participant's money belongs to whoever they were merged into.
        current = participant_id
        for _ in range(MAX_MERGE_DEPTH):
            if current is None or current not in merged:
                return current
            current = merged[current]
        raise RuntimeError("participant merge chain is too deep")

    lines = [
        replace(line, participant_id=holder(line.participant_id))
        for line in _expenses(entities.get("expense", []))
    ]
    return lines


def spending_totals(lines: Iterable[SpendingLine]) -> dict[tuple[str | None, str], list[int]]:
    """``[paid, owed]`` per (participant, currency)."""

    totals: dict[tuple[str | None, str], list[int]] = {}
    for line in lines:
        found = totals.setdefault((line.participant_id, line.currency), [0, 0])
        found[0] += line.paid_minor
        found[1] += line.owed_minor
    return totals


def _expenses(expenses: list[dict[str, Any]]) -> Iterator[SpendingLine]:
    for expense in expenses:
        if expense["state"] != "active":
            continue  # a voided expense moves no money
        revision = expense["revision"]
        currency = revision["currency"]
        for payer in revision["payers"]:
            who = KITTY if payer["fund"] else payer["participant_id"]
            yield SpendingLine(who, currency, payer["amount_minor"], 0)
        for share in revision["shares"]:
            yield SpendingLine(share["participant_id"], currency, 0, share["owed_minor"])
        for refund in expense["refunds"]:
            if refund["reversed"]:
                continue
            # The money came back to the recipient; the sharers owe that much less.
            recipient = KITTY if refund["fund"] else refund["recipient_participant_id"]
            yield SpendingLine(recipient, refund["currency"], -refund["amount_minor"], 0)
            for share in refund["shares"]:
                yield SpendingLine(
                    share["participant_id"], refund["currency"], 0, -share["amount_minor"]
                )


async def _revisions(
    ctx: CommandContext, ids: set[UUID | None]
) -> dict[UUID, tuple[str, str, date]]:
    wanted = [value for value in ids if value is not None]
    if not wanted:
        return {}
    rows = await ctx.session.execute(
        select(
            ExpenseRevision.id,
            ExpenseRevision.description,
            ExpenseRevision.category,
            ExpenseRevision.occurred_on,
        ).where(ExpenseRevision.id.in_(wanted))
    )
    return {row.id: (row.description, row.category, row.occurred_on) for row in rows}


async def _dates(
    ctx: CommandContext, model: type[Settlement] | type[FundMovement], ids: set[UUID | None]
) -> dict[UUID, date]:
    wanted = [value for value in ids if value is not None]
    if not wanted:
        return {}
    rows = await ctx.session.execute(
        select(model.id, model.occurred_on).where(model.id.in_(wanted))
    )
    return {row.id: row.occurred_on for row in rows}


def _local_day(moment: datetime, zone: ZoneInfo) -> date:
    return moment.astimezone(zone).date()
