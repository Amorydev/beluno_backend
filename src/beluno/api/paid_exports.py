"""Paid exports of a trip: the accounting CSV and the PDF trip report.

Both need a Trip Pass for the trip or Pro for its owner (hangouts are always free;
the report is for trips only), checked before any data is read. Like the free
exports, they hold only what the caller can see, are read inside the request's
transaction, and render after it closes.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from uuid import UUID

from beluno.api import accounting, report_text, trip_report
from beluno.api.exports import (
    ExportFile,
    audit_export,
    currency_exponents,
    names_of,
    plan_rows,
)
from beluno.api.report_text import Language
from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.common import spreadsheet_text
from beluno.contracts.errors import conflict
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan
from beluno.modules import billing
from beluno.modules.account_deletion import FORMER_MEMBER
from beluno.modules.context import CommandContext
from beluno.modules.finance.views import ledger_snapshot
from beluno.modules.recap import get_recap

ACCOUNTING_COLUMNS = (
    "date",
    "entry",
    "description",
    "category",
    "person_id",
    "person",
    "currency",
    "amount",
    "ledger_seq",
    "reference_id",
)


async def plan_accounting_csv(ctx: CommandContext, plan_id: UUID) -> ExportFile:
    """Paid feature: one row per account and journal entry, adding up to the balances."""

    plan = await _paid_plan(ctx, plan_id, "The accounting export", trips_only=False)
    names = names_of(await plan_rows(ctx, plan_id, "plan_participant"))
    lines = await accounting.journal_lines(ctx, plan_id, plan.timezone)
    exponents = await currency_exponents(ctx)
    await audit_export(ctx, plan_id, "accounting")

    def render() -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(ACCOUNTING_COLUMNS)
        for line in lines:
            amount = Decimal(line.amount_minor).scaleb(-exponents.get(line.currency, 2))
            writer.writerow(
                [
                    line.occurred_on,
                    line.entry,
                    spreadsheet_text(line.description),
                    line.category,
                    line.participant_id or "",
                    spreadsheet_text(names.get(line.participant_id, "Former member")),
                    line.currency,
                    str(amount),
                    line.ledger_seq,
                    line.reference,
                ]
            )
        return buffer.getvalue().encode("utf-8-sig")

    return ExportFile(f"beluno-accounting-{plan_id}.csv", "text/csv; charset=utf-8", render)


async def plan_pdf(
    ctx: CommandContext, plan_id: UUID, language: Language | None = None
) -> ExportFile:
    """Paid feature: the trip report (spending, people, settling up, expenses), in
    ``language`` or the caller's own (their profile's locale)."""

    plan = await _paid_plan(ctx, plan_id, "The trip report", trips_only=True)
    if language is None:
        caller = await ctx.session.get(User, ctx.require_actor().user_id)
        language = report_text.language_for(caller.locale if caller else None)
    entities = await plan_rows(ctx, plan_id, "plan_participant", "expense", "media")
    recap = await get_recap(ctx, plan_id)
    snapshot = await ledger_snapshot(ctx, plan_id)
    exponents = await currency_exponents(ctx)
    await audit_export(ctx, plan_id, "pdf")
    clean = trip_report.printable
    former = report_text.text("former_member", language)
    # Deleted accounts carry a stored English name; the reader sees their own words.
    names = {
        key: former if value == FORMER_MEMBER else clean(value)
        for key, value in names_of(entities).items()
    }
    names[None] = report_text.text("kitty", language)
    balances = {
        (
            str(view.account.participant_id) if view.account.participant_id else None,
            view.account.currency,
        ): view.balance_minor
        for view in snapshot.accounts
    }
    receipts = {
        row["expense_id"]
        for row in entities.get("media", [])
        if row["kind"] == "receipt" and row["state"] == "ready" and row["expense_id"]
    }

    def money(minor: int | None, currency: str) -> str:
        return "" if minor is None else report_text.money(minor, currency, exponents, language)

    def who(participant_id: str | None) -> str:
        return names.get(participant_id, former)

    expenses = sorted(
        (row for row in entities.get("expense", []) if row["state"] == "active"),
        key=lambda row: (row["revision"]["occurred_on"], row["id"]),
    )
    shown = expenses[: trip_report.MAX_EXPENSE_ROWS]
    report = trip_report.TripReport(
        title=clean(plan.title),
        dates=report_text.dates(recap.start, recap.end, recap.days, language),
        stops=" · ".join(clean(stop.name) for stop in recap.stops),
        people=recap.people,
        base_currency=recap.currency,
        spent=money(recap.spent_minor, recap.currency),
        spending_note=" ".join(
            report_text.text(key, language)
            for key, applies in (
                ("unconverted", recap.unconverted),
                ("estimated", recap.estimated_rates),
            )
            if applies
        )
        or None,
        categories=[
            (
                report_text.category(category, language),
                money(spent, recap.currency),
                f"{points / 100:.0f}%",
            )
            for category, spent, points in recap.categories
        ],
        people_rows=trip_report.person_rows(
            accounting.spending_lines(entities), balances, names, exponents, language
        ),
        transfers=[
            trip_report.Transfer(
                who(str(transfer.from_participant_id)),
                who(str(transfer.to_participant_id)),
                money(transfer.amount_minor, preview.currency),
            )
            for preview in snapshot.suggestions
            for transfer in preview.preview.transfers
        ],
        expenses=[
            trip_report.ExpenseRow(
                occurred_on=report_text.day(
                    date.fromisoformat(row["revision"]["occurred_on"]), language
                ),
                description=clean(row["revision"]["description"]),
                category=report_text.category(row["revision"]["category"], language),
                amount=money(row["revision"]["amount_minor"], row["revision"]["currency"]),
                base_amount=money(
                    row["revision"]["base"]["amount_minor"], row["revision"]["base"]["currency"]
                ),
                paid_by=", ".join(
                    who(None) if payer["fund"] else who(payer["participant_id"])
                    for payer in row["revision"]["payers"]
                ),
                receipt=row["id"] in receipts,
            )
            for row in shown
        ],
        more_expenses=len(expenses) - len(shown),
        generated_on=report_text.day(ctx.now.date(), language),
        language=language,
    )
    return ExportFile(
        f"beluno-trip-{plan_id}.pdf",
        "application/pdf",
        lambda: trip_report.render(report),
        limiter=trip_report.RENDERS,
    )


async def _paid_plan(ctx: CommandContext, plan_id: UUID, what: str, *, trips_only: bool) -> Plan:
    """The plan, once the caller may see its money and the trip is unlocked."""

    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    if trips_only and access.plan.type != "trip":
        raise conflict("NOT_AVAILABLE_FOR_HANGOUT", f"{what} is for trips")
    await billing.require_unlocked(ctx, plan_id, access.plan.type, what)
    return access.plan
