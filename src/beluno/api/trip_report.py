"""The PDF trip report: where the money went, who paid what, and who still owes whom.

A cover (title, dates, stops, people), spending by category, each person's paid and
share per currency with their balance, the suggested transfers, and every expense by
date (marking those with a receipt). Rendering is pure: ``render`` runs in a worker
thread after the request's transaction has closed. Text uses Noto Sans (embedded,
subset), so Vietnamese and other Latin, Greek, and Cyrillic names print correctly;
characters it lacks (Thai, CJK, Arabic, Hebrew, emoji) print as ``?``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path

import anyio
from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from beluno.api.accounting import SpendingLine, spending_totals

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FAMILY = "NotoSans"
INK = (33, 37, 41)
MUTED = (108, 117, 125)
RULE = (222, 226, 230)
# The expenses table stops here (the accounting CSV has every entry): rendering is pure
# Python and holds the interpreter while it runs.
MAX_EXPENSE_ROWS = 1_000
# At most this many reports render at once, in worker threads.
RENDERS = anyio.CapacityLimiter(2)
MISSING = "?"


@dataclass(frozen=True)
class ExpenseRow:
    occurred_on: str
    description: str
    category: str
    amount: str  # in the expense's currency
    base_amount: str  # in the plan's base currency, when converted
    paid_by: str
    receipt: bool


@dataclass(frozen=True)
class Transfer:
    debtor: str
    creditor: str
    amount: str


@dataclass(frozen=True)
class PersonRow:
    name: str
    currency: str
    paid: str
    share: str
    balance: str


@dataclass(frozen=True)
class TripReport:
    title: str
    dates: str
    stops: str
    people: int
    base_currency: str
    spent: str
    spending_note: str | None
    categories: list[tuple[str, str, str]]  # category, amount, share of the total
    people_rows: list[PersonRow]
    transfers: list[Transfer]
    expenses: list[ExpenseRow]  # at most MAX_EXPENSE_ROWS
    more_expenses: int  # left out of the table
    generated_on: date


def money(minor: int, currency: str, exponents: dict[str, int]) -> str:
    """``1234567`` USD -> ``12,345.67 USD``."""

    exponent = exponents.get(currency, 2)
    value = Decimal(minor).scaleb(-exponent)
    return f"{value:,.{exponent}f} {currency}"


def person_rows(
    lines: list[SpendingLine],
    balances: dict[tuple[str | None, str], int],
    names: dict[str | None, str],
    exponents: dict[str, int],
) -> list[PersonRow]:
    """Paid and share of expenses (refunds netted) per person and currency, and the
    ledger balance, which also counts payments, forgiven debts, and the kitty."""

    spent = spending_totals(lines)
    keys = sorted(
        set(spent) | {key for key, value in balances.items() if value},
        key=lambda key: (key[0] is None, names.get(key[0], ""), key[1]),
    )
    return [
        PersonRow(
            names.get(participant_id, "Former member"),
            currency,
            money(spent.get((participant_id, currency), [0, 0])[0], currency, exponents),
            money(spent.get((participant_id, currency), [0, 0])[1], currency, exponents),
            money(balances.get((participant_id, currency), 0), currency, exponents),
        )
        for participant_id, currency in keys
    ]


@cache
def _printable_codepoints() -> frozenset[int]:
    found: set[int] = set()
    for name in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        cmap = TTFont(str(FONTS / name)).getBestCmap()
        found = set(cmap) if not found else found & set(cmap)
    return frozenset(found)


def printable(text: str) -> str:
    """People's text with every character the font lacks shown as ``?``."""

    known = _printable_codepoints()
    return "".join(char if ord(char) in known else MISSING for char in text)


def render(report: TripReport) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_title(report.title)
    pdf.set_creator("Beluno")
    pdf.add_font(FAMILY, "", str(FONTS / "NotoSans-Regular.ttf"))
    pdf.add_font(FAMILY, "B", str(FONTS / "NotoSans-Bold.ttf"))
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.add_page()
    pdf.set_text_color(*INK)

    pdf.set_font(FAMILY, "B", 22)
    pdf.multi_cell(0, 10, report.title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(FAMILY, "", 11)
    pdf.set_text_color(*MUTED)
    for detail in (
        report.dates,
        report.stops,
        f"{report.people} people · amounts in {report.base_currency} unless marked",
    ):
        if detail:
            pdf.multi_cell(0, 6, detail, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*INK)
    pdf.ln(4)
    pdf.set_font(FAMILY, "B", 16)
    pdf.cell(0, 9, f"Spent: {report.spent}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if report.spending_note:
        _note(pdf, report.spending_note)

    _heading(pdf, "By category")
    _table(
        pdf,
        ("Category", "Amount", "Share"),
        [list(row) for row in report.categories],
        (2, 1.4, 0.8),
    )

    _heading(pdf, "People")
    _table(
        pdf,
        ("Person", "Currency", "Paid", "Share", "Balance"),
        [[row.name, row.currency, row.paid, row.share, row.balance] for row in report.people_rows],
        (1.6, 0.8, 1.2, 1.2, 1.2),
    )
    _note(pdf, "Balance: positive gets money back, negative owes. It counts payments too.")

    _heading(pdf, "To settle up")
    if report.transfers:
        _table(
            pdf,
            ("From", "To", "Amount"),
            [[row.debtor, row.creditor, row.amount] for row in report.transfers],
            (1.4, 1.4, 1.2),
        )
    else:
        _note(pdf, "Nobody owes anybody.")

    _heading(pdf, "Expenses")
    _table(
        pdf,
        ("Date", "Description", "Category", "Amount", "Base", "Paid by", "Receipt"),
        [
            [
                row.occurred_on,
                row.description,
                row.category,
                row.amount,
                row.base_amount,
                row.paid_by,
                "yes" if row.receipt else "",
            ]
            for row in report.expenses
        ],
        (0.9, 2, 1, 1.2, 1.2, 1.4, 0.6),
        size=8,
    )
    if report.more_expenses:
        _note(
            pdf,
            f"And {report.more_expenses} more expenses: the accounting export lists them all.",
        )
    pdf.ln(4)
    _note(pdf, f"Made with Beluno on {report.generated_on.isoformat()}.")
    return bytes(pdf.output())


def _heading(pdf: FPDF, text: str) -> None:
    pdf.ln(6)
    pdf.set_font(FAMILY, "B", 13)
    pdf.cell(0, 8, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _note(pdf: FPDF, text: str) -> None:
    pdf.set_font(FAMILY, "", 9)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*INK)


def _table(
    pdf: FPDF,
    headings: tuple[str, ...],
    rows: list[list[str]],
    widths: tuple[float, ...],
    size: int = 9,
) -> None:
    if not rows:
        _note(pdf, "Nothing yet.")
        return
    pdf.set_font(FAMILY, "", size)
    pdf.set_draw_color(*RULE)
    with pdf.table(
        col_widths=widths,
        headings_style=FontFace(family=FAMILY, emphasis="BOLD", size_pt=size),
        line_height=size * 0.55,
        borders_layout="HORIZONTAL_LINES",
        text_align="LEFT",
        first_row_as_headings=True,
    ) as table:
        table.row(headings)
        for values in rows:
            table.row(values)
