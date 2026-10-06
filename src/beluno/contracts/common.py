"""Reusable validated field types and page envelopes for public contracts."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Generic, TypeVar
from zoneinfo import available_timezones

from pydantic import AfterValidator, BaseModel, StringConstraints


def clean_text(value: str) -> str:
    """Collapse internal whitespace and trim; reject strings that become empty."""

    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


def strip_optional_text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


@lru_cache(maxsize=1)
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def validate_timezone(value: str) -> str:
    if value not in _timezones():
        raise ValueError("must be an IANA timezone name")
    return value


DisplayName = Annotated[
    str,
    StringConstraints(max_length=200),
    AfterValidator(clean_text),
    StringConstraints(min_length=1, max_length=80),
]
Title = Annotated[
    str,
    StringConstraints(max_length=400),
    AfterValidator(clean_text),
    StringConstraints(min_length=1, max_length=120),
]
GroupName = Annotated[
    str,
    StringConstraints(max_length=200),
    AfterValidator(clean_text),
    StringConstraints(min_length=1, max_length=80),
]
LongText = Annotated[str, AfterValidator(strip_optional_text), StringConstraints(max_length=2000)]
Label = Annotated[str, AfterValidator(clean_text), StringConstraints(max_length=200)]
TimezoneName = Annotated[str, StringConstraints(max_length=64), AfterValidator(validate_timezone)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
LocaleTag = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$", max_length=35)
]

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    next_cursor: str | None = None
