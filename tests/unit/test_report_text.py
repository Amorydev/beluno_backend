from __future__ import annotations

import string
from datetime import date

from beluno.api.report_text import (
    CATEGORIES,
    LANGUAGES,
    TEXT,
    category,
    dates,
    language_for,
    money,
    text,
)

EXPONENTS = {"USD": 2, "VND": 0, "KWD": 3}


def test_money_reads_the_way_each_language_writes_it() -> None:
    assert money(123_456, "USD", EXPONENTS, "en") == "1,234.56 USD"
    assert money(123_456, "USD", EXPONENTS, "vi") == "1.234,56 USD"
    assert money(1_200_000, "VND", EXPONENTS, "vi") == "1.200.000 VND"
    assert money(-5, "KWD", EXPONENTS, "en") == "-0.005 KWD"
    assert money(-123_456, "USD", EXPONENTS, "vi") == "-1.234,56 USD"
    assert money(0, "VND", EXPONENTS, "vi") == "0 VND"


def test_days_categories_and_the_readers_language() -> None:
    start, end = date(2027, 3, 12), date(2027, 3, 18)
    assert dates(start, end, 7, "en") == "12 Mar 2027 \N{EN DASH} 18 Mar 2027 \N{MIDDLE DOT} 7 days"
    assert dates(start, end, 7, "vi") == "12/03/2027 \N{EN DASH} 18/03/2027 \N{MIDDLE DOT} 7 ngày"
    assert dates(start, None, None, "vi") == "12/03/2027"
    assert dates(None, None, None, "vi") == "Chưa đặt ngày"
    assert (category("food", "vi"), category("food", "en"), category("new_one", "vi")) == (
        "Ăn uống",
        "Food",
        "New one",
    )
    assert [language_for(locale) for locale in ("vi", "vi-VN", "vi_VN", "en-US", None, "")] == [
        "vi",
        "vi",
        "vi",
        "en",
        "en",
        "en",
    ]
    assert text("more_expenses", "vi", count=3).startswith("Còn 3 khoản chi")


def test_every_word_exists_in_both_languages_with_the_same_blanks() -> None:
    def blanks(value: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(value) if name}

    for key, words in {**TEXT, **CATEGORIES}.items():
        assert set(words) == set(LANGUAGES), key
        assert blanks(words["vi"]) == blanks(words["en"]), key
