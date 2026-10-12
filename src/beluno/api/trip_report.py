"""The PDF trip report: where the money went, who paid what, and who still owes whom.

A cover (title, dates, stops, people), spending by category, each person's paid and
share per currency with their balance, the suggested transfers, and every expense by
date (marking those with a receipt). Rendering is pure: ``render`` runs in a worker
thread after the request's transaction has closed. Text uses Noto Sans (embedded,
subset), so Vietnamese and other Latin, Greek, and Cyrillic names print correctly;
characters it lacks (Thai, CJK, Arabic, Hebrew, emoji) print as ``?``. The report is
in Vietnamese or English (``beluno.api.report_text``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import anyio
from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from beluno.api.accounting import SpendingLine, spending_totals
from beluno.api.report_text import Language, money, text

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
    generated_on: str
    language: Language


def person_rows(
    lines: list[SpendingLine],
    balances: dict[tuple[str | None, str], int],
    names: dict[str | None, str],
    exponents: dict[str, int],
    language: Language,
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
            names.get(participant_id, text("former_member", language)),
            currency,
            money(spent.get((participant_id, currency), [0, 0])[0], currency, exponents, language),
            money(spent.get((participant_id, currency), [0, 0])[1], currency, exponents, language),
            money(balances.get((participant_id, currency), 0), currency, exponents, language),
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
    language = report.language

    def say(key: str, **values: object) -> str:
        return text(key, language, **values)

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_title(report.title)
    pdf.set_lang(language)
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
        say("people_line", people=report.people, currency=report.base_currency),
    ):
        if detail:
            pdf.multi_cell(0, 6, detail, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*INK)
    pdf.ln(4)
    pdf.set_font(FAMILY, "B", 16)
    pdf.cell(0, 9, say("spent", amount=report.spent), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if report.spending_note:
        _note(pdf, report.spending_note)

    nothing = say("nothing_yet")
    _heading(pdf, say("by_category"))
    _table(
        pdf,
        (say("category"), say("amount"), say("share_of_total")),
        [list(row) for row in report.categories],
        (2, 1.4, 0.8),
        nothing,
    )

    _heading(pdf, say("people"))
    _table(
        pdf,
        (say("person"), say("currency"), say("paid"), say("share"), say("balance")),
        [[row.name, row.currency, row.paid, row.share, row.balance] for row in report.people_rows],
        (1.6, 0.8, 1.2, 1.2, 1.2),
        nothing,
    )
    _note(pdf, say("balance_note"))

    _heading(pdf, say("settle_up"))
    if report.transfers:
        _table(
            pdf,
            (say("from"), say("to"), say("amount")),
            [[row.debtor, row.creditor, row.amount] for row in report.transfers],
            (1.4, 1.4, 1.2),
            nothing,
        )
    else:
        _note(pdf, say("nobody_owes"))

    _heading(pdf, say("expenses"))
    _table(
        pdf,
        (
            say("date"),
            say("description"),
            say("category"),
            say("amount"),
            say("base"),
            say("paid_by"),
            say("receipt"),
        ),
        [
            [
                row.occurred_on,
                row.description,
                row.category,
                row.amount,
                row.base_amount,
                row.paid_by,
                say("yes") if row.receipt else "",
            ]
            for row in report.expenses
        ],
        (0.9, 1.85, 1, 1.2, 1.2, 1.4, 0.75),
        nothing,
        size=8,
    )
    if report.more_expenses:
        _note(pdf, say("more_expenses", count=report.more_expenses))
    pdf.ln(4)
    _note(pdf, say("made_on", day=report.generated_on))
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
    empty: str,
    size: int = 9,
) -> None:
    if not rows:
        _note(pdf, empty)
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
