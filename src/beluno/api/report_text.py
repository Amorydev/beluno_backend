"""The PDF trip report's words, in Vietnamese and English, and how it writes money and days.

Vietnamese groups thousands with dots and uses a decimal comma (``1.234,56 USD``) and
writes days as ``12/03/2027``; English uses ``1,234.56 USD`` and ``12 Mar 2027``.
People's own text (titles, names, descriptions) is never translated.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

Language = Literal["vi", "en"]
LANGUAGES: tuple[Language, ...] = ("vi", "en")

TEXT: dict[str, dict[Language, str]] = {
    "people_line": {
        "en": "{people} people · amounts in {currency} unless marked",
        "vi": "{people} người · số tiền tính bằng {currency} trừ khi ghi khác",
    },
    "spent": {"en": "Spent: {amount}", "vi": "Đã chi: {amount}"},
    "by_category": {"en": "By category", "vi": "Theo hạng mục"},
    "category": {"en": "Category", "vi": "Hạng mục"},
    "amount": {"en": "Amount", "vi": "Số tiền"},
    "share_of_total": {"en": "Share", "vi": "Tỷ lệ"},
    "people": {"en": "People", "vi": "Thành viên"},
    "person": {"en": "Person", "vi": "Tên"},
    "currency": {"en": "Currency", "vi": "Tiền tệ"},
    "paid": {"en": "Paid", "vi": "Đã trả"},
    "share": {"en": "Share", "vi": "Phần chia"},
    "balance": {"en": "Balance", "vi": "Số dư"},
    "balance_note": {
        "en": "Balance: positive gets money back, negative owes. It counts payments too.",
        "vi": "Số dư: dương là được nhận lại, âm là còn nợ. Đã tính cả các khoản đã trả nhau.",
    },
    "settle_up": {"en": "To settle up", "vi": "Cần thanh toán"},
    "from": {"en": "From", "vi": "Người trả"},
    "to": {"en": "To", "vi": "Người nhận"},
    "nobody_owes": {"en": "Nobody owes anybody.", "vi": "Không ai nợ ai."},
    "expenses": {"en": "Expenses", "vi": "Khoản chi"},
    "date": {"en": "Date", "vi": "Ngày"},
    "description": {"en": "Description", "vi": "Mô tả"},
    "base": {"en": "Base", "vi": "Quy đổi"},
    "paid_by": {"en": "Paid by", "vi": "Người trả"},
    "receipt": {"en": "Receipt", "vi": "Hóa đơn"},
    "yes": {"en": "yes", "vi": "có"},
    "more_expenses": {
        "en": "And {count} more expenses: the accounting export lists them all.",
        "vi": "Còn {count} khoản chi khác: file CSV kế toán liệt kê đầy đủ.",
    },
    "made_on": {"en": "Made with Beluno on {day}.", "vi": "Tạo bằng Beluno ngày {day}."},
    "nothing_yet": {"en": "Nothing yet.", "vi": "Chưa có gì."},
    "dates_not_set": {"en": "Dates not set", "vi": "Chưa đặt ngày"},
    "days": {"en": "{days} days", "vi": "{days} ngày"},
    "unconverted": {
        "en": "Some expenses have no rate to the base currency yet and are left out.",
        "vi": "Một số khoản chi chưa có tỷ giá sang tiền tệ chính nên chưa được tính.",
    },
    "estimated": {"en": "Some rates are estimates.", "vi": "Một số tỷ giá là ước tính."},
    "former_member": {"en": "Former member", "vi": "Thành viên cũ"},
    "kitty": {"en": "Kitty", "vi": "Quỹ chung"},
}

# Thousands with dots, decimals with a comma.
VIETNAMESE_DIGITS = str.maketrans(",.", ".,")

CATEGORIES: dict[str, dict[Language, str]] = {
    "food": {"en": "Food", "vi": "Ăn uống"},
    "lodging": {"en": "Lodging", "vi": "Chỗ ở"},
    "transport": {"en": "Transport", "vi": "Di chuyển"},
    "activities": {"en": "Activities", "vi": "Hoạt động"},
    "shopping": {"en": "Shopping", "vi": "Mua sắm"},
    "groceries": {"en": "Groceries", "vi": "Đi chợ"},
    "fees": {"en": "Fees", "vi": "Phí"},
    "other": {"en": "Other", "vi": "Khác"},
}


def language_for(locale: str | None) -> Language:
    """Vietnamese for a ``vi`` locale (``vi``, ``vi-VN``), English otherwise."""

    return "vi" if (locale or "").lower().split("-")[0].split("_")[0] == "vi" else "en"


def text(key: str, language: Language, **values: object) -> str:
    return TEXT[key][language].format(**values)


def category(name: str, language: Language) -> str:
    found = CATEGORIES.get(name)
    return found[language] if found else name.replace("_", " ").capitalize()


def money(minor: int, currency: str, exponents: dict[str, int], language: Language) -> str:
    """``123456`` USD: ``1,234.56 USD`` in English, ``1.234,56 USD`` in Vietnamese."""

    exponent = exponents.get(currency, 2)
    written = f"{Decimal(minor).scaleb(-exponent):,.{exponent}f}"
    if language == "vi":
        written = written.translate(VIETNAMESE_DIGITS)
    return f"{written} {currency}"


# English month names never depend on the server's locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day(value: date, language: Language) -> str:
    if language == "vi":
        return f"{value.day:02d}/{value.month:02d}/{value.year}"
    return f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}"


def dates(start: date | None, end: date | None, days: int | None, language: Language) -> str:
    if start is None:
        return text("dates_not_set", language)
    if end is None or end == start:
        return day(start, language)
    span = f"{day(start, language)} \N{EN DASH} {day(end, language)}"
    return f"{span} \N{MIDDLE DOT} {text('days', language, days=days)}"
