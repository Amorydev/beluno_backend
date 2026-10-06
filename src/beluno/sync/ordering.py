"""Fractional ordering keys for client-side reordering without renumbering.

Keys are strings over a 62-character alphabet that sort lexicographically as
fractions in ``[0, 1)``. A new key can always be generated between any two
existing keys (or at either end) without touching other rows, so two devices
inserting offline do not conflict. ``rebalance`` produces a short, evenly
spaced key set when keys have grown long after many insertions.

Keys never end with the smallest digit, which keeps "between" always possible
and makes each fraction's representation unique.
"""

from __future__ import annotations

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
MAX_KEY_LENGTH = 128
_DIGIT = {char: index for index, char in enumerate(ALPHABET)}


class OrderingKeyError(ValueError):
    """The key is malformed or the requested gap does not exist."""


def is_valid_key(key: str) -> bool:
    return (
        0 < len(key) <= MAX_KEY_LENGTH
        and all(char in _DIGIT for char in key)
        and key[-1] != ALPHABET[0]
    )


def _require_valid(key: str | None) -> None:
    if key is not None and not is_valid_key(key):
        raise OrderingKeyError(f"invalid ordering key {key!r}")


def key_between(lower: str | None, upper: str | None) -> str:
    """A key strictly between ``lower`` and ``upper``; ``None`` means the open end."""

    _require_valid(lower)
    _require_valid(upper)
    if lower is not None and upper is not None and lower >= upper:
        raise OrderingKeyError("lower must sort before upper")
    key = _between(lower or "", upper)
    if len(key) > MAX_KEY_LENGTH:
        raise OrderingKeyError("keys are too long; rebalance the sequence")
    return key


def _between(lower: str, upper: str | None) -> str:
    index = 0
    if upper is not None:
        while index < len(lower) and index < len(upper) and lower[index] == upper[index]:
            index += 1
    prefix = lower[:index]
    low_digit = _DIGIT[lower[index]] if index < len(lower) else 0
    high_digit = _DIGIT[upper[index]] if upper is not None and index < len(upper) else BASE
    if high_digit - low_digit >= 2:
        return prefix + ALPHABET[(low_digit + high_digit) // 2]
    if high_digit == low_digit:
        # ``lower`` is exhausted and ``upper`` continues with the smallest digit.
        assert upper is not None
        return prefix + ALPHABET[low_digit] + _between("", upper[index + 1 :])
    # Adjacent digits: extend below the lower key's remainder with no upper bound.
    return prefix + ALPHABET[low_digit] + _between(lower[index + 1 :], None)


def first_key() -> str:
    return key_between(None, None)


def rebalance(count: int) -> list[str]:
    """``count`` short, evenly spaced keys in ascending order (deterministic)."""

    if count < 0:
        raise OrderingKeyError("count must not be negative")
    if count == 0:
        return []
    length = 1
    while BASE**length < count + 1:
        length += 1
    space = BASE**length
    keys = []
    for position in range(1, count + 1):
        value = position * space // (count + 1)
        digits = []
        for _ in range(length):
            value, remainder = divmod(value, BASE)
            digits.append(ALPHABET[remainder])
        keys.append("".join(reversed(digits)).rstrip(ALPHABET[0]))
    return keys
