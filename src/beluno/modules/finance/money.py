"""Money rules shared by every finance command.

Amounts are integers in minor units of a currency whose exponent is pinned in
``finance.currencies``; floating point never touches money. Bounds keep every
stored value, sum, and conversion far inside ``BIGINT``.
"""

from __future__ import annotations

from beluno.modules.finance.errors import amount_out_of_range

MAX_AMOUNT_MINOR = 10**12
MAX_SPLIT_PARTICIPANTS = 100
MAX_PAYERS = 100
MAX_ITEMS = 100
MAX_EXTRAS = 10
MAX_WEIGHT = 10**6
BASIS_POINTS_TOTAL = 10_000


def check_amount(value: int, *, field: str) -> int:
    """A strictly positive amount within the supported bound."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer number of minor units")
    if value <= 0 or value > MAX_AMOUNT_MINOR:
        raise amount_out_of_range(f"{field} must be between 1 and {MAX_AMOUNT_MINOR} minor units")
    return value
