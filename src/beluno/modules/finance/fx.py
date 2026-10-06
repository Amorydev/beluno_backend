"""Exchange-rate snapshots and minor-unit conversion.

A rate is the number of quote-currency units for one base-currency unit (major
units, e.g. ``EUR→USD 1.0823``). Conversion scales by both exponents and rounds
half-even once, at the end, with exact decimal arithmetic.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum

from beluno.modules.finance.errors import amount_out_of_range, fx_rate_invalid
from beluno.modules.finance.money import MAX_AMOUNT_MINOR

RATE_DECIMALS = 12
MAX_RATE = Decimal(10) ** 9
_RATE_QUANTUM = Decimal(1).scaleb(-RATE_DECIMALS)
_PRECISION = 60


class RateSource(StrEnum):
    MANUAL = "manual"
    ESTIMATED = "estimated"
    # Implied by both amounts of a cross-currency settlement the parties agreed on.
    AGREED = "agreed"


def parse_rate(raw: str | Decimal) -> Decimal:
    """A positive rate with at most 12 decimals and at most ``10^9``."""

    try:
        rate = Decimal(raw) if not isinstance(raw, Decimal) else raw
    except InvalidOperation as error:
        raise fx_rate_invalid("rate must be a decimal number") from error
    if not rate.is_finite() or rate <= 0 or rate > MAX_RATE:
        raise fx_rate_invalid(f"rate must be greater than 0 and at most {MAX_RATE}")
    exponent = rate.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -RATE_DECIMALS:
        raise fx_rate_invalid(f"rate takes at most {RATE_DECIMALS} decimals")
    return rate


def convert(amount_minor: int, *, from_exponent: int, to_exponent: int, rate: Decimal) -> int:
    """Convert minor units with one half-even rounding; refuse results beyond the bound."""

    with localcontext() as context:
        context.prec = _PRECISION
        value = Decimal(amount_minor) * rate * Decimal(10) ** (to_exponent - from_exponent)
        converted = int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if abs(converted) > MAX_AMOUNT_MINOR:
        raise amount_out_of_range("the converted amount exceeds the supported bound")
    return converted


def implied_rate(
    from_minor: int, *, from_exponent: int, to_minor: int, to_exponent: int
) -> Decimal:
    """The rate two agreed amounts imply, rounded half-even to 12 decimals."""

    if from_minor <= 0 or to_minor <= 0:
        raise ValueError("amounts must be positive")
    with localcontext() as context:
        context.prec = _PRECISION
        rate = (Decimal(to_minor).scaleb(-to_exponent)) / (
            Decimal(from_minor).scaleb(-from_exponent)
        )
        rate = rate.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
    if rate <= 0 or rate > MAX_RATE:
        raise fx_rate_invalid("the agreed amounts imply an unsupported rate")
    return rate
