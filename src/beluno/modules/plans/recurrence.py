"""Validated recurrence rules for plan series (an RFC 5545 RRULE subset).

Occurrences are computed on local calendar *dates*, so DST never moves them to a
different day; the local start time is applied afterwards with the series'
IANA zone. Accepted keys: FREQ (DAILY/WEEKLY/MONTHLY), INTERVAL, BYDAY,
BYMONTHDAY, and either COUNT or UNTIL (a date).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from dateutil.rrule import rrulestr

from beluno.contracts.errors import validation_error

KEY_ORDER = ("FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL")
FREQUENCIES = frozenset({"DAILY", "WEEKLY", "MONTHLY"})
BYDAY_PATTERN = re.compile(r"^([+-]?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")
UNTIL_PATTERN = re.compile(r"^\d{8}$")
MAX_OCCURRENCES_PER_RUN = 60


def normalize_rule(raw: str) -> str:
    """Validate a rule and return its canonical form (stable key order, upper case)."""

    text = raw.strip().upper().removeprefix("RRULE:")
    parts: dict[str, str] = {}
    for item in filter(None, text.split(";")):
        key, separator, value = item.partition("=")
        if not separator or key not in KEY_ORDER or key in parts or not value:
            raise validation_error("recurrence_rule uses an unsupported or repeated key")
        parts[key] = value
    if parts.get("FREQ") not in FREQUENCIES:
        raise validation_error("recurrence_rule FREQ must be DAILY, WEEKLY, or MONTHLY")
    _check_int(parts, "INTERVAL", 1, 52)
    _check_int(parts, "COUNT", 1, 520)
    if "COUNT" in parts and "UNTIL" in parts:
        raise validation_error("recurrence_rule cannot combine COUNT and UNTIL")
    if "UNTIL" in parts:
        if not UNTIL_PATTERN.match(parts["UNTIL"]):
            raise validation_error("recurrence_rule UNTIL must be a date (YYYYMMDD)")
        try:
            datetime.strptime(parts["UNTIL"], "%Y%m%d")
        except ValueError as error:
            raise validation_error("recurrence_rule UNTIL is not a valid date") from error
    if "BYDAY" in parts and not all(BYDAY_PATTERN.match(day) for day in parts["BYDAY"].split(",")):
        raise validation_error("recurrence_rule BYDAY is invalid")
    if "BYMONTHDAY" in parts:
        for day in parts["BYMONTHDAY"].split(","):
            if not re.fullmatch(r"-?\d{1,2}", day) or int(day) == 0 or abs(int(day)) > 31:
                raise validation_error("recurrence_rule BYMONTHDAY is invalid")
    canonical = ";".join(f"{key}={parts[key]}" for key in KEY_ORDER if key in parts)
    try:
        rrulestr(canonical, dtstart=datetime(2000, 1, 1))
    except (ValueError, TypeError) as error:
        raise validation_error("recurrence_rule is invalid") from error
    return canonical


def occurrence_dates(
    rule: str,
    *,
    start_date: date,
    window_start: date,
    window_end: date,
    limit: int = MAX_OCCURRENCES_PER_RUN,
) -> list[date]:
    """Dates in ``[window_start, window_end]`` produced by the rule, at most ``limit``."""

    if window_end < window_start:
        return []
    recurrence = rrulestr(rule, dtstart=datetime.combine(start_date, time()))
    found: list[date] = []
    for moment in recurrence.xafter(datetime.combine(window_start, time()), inc=True):
        day = moment.date()
        if day > window_end or len(found) >= limit:
            break
        found.append(day)
    return found


def end_before(rule: str, boundary: date) -> str:
    """The same rule ending on the day before ``boundary`` (for this-and-future splits)."""

    parts = [part for part in rule.split(";") if not part.startswith(("COUNT=", "UNTIL="))]
    parts.append(f"UNTIL={(boundary - timedelta(days=1)).strftime('%Y%m%d')}")
    return normalize_rule(";".join(parts))


def continuation_rule(rule: str, *, start_date: date, boundary: date) -> str:
    """The rule for a successor series starting at ``boundary``.

    UNTIL is an absolute date and carries over; COUNT is reduced by the
    occurrences that already happened before ``boundary`` so a split never adds
    extra occurrences.
    """

    parts = dict(part.split("=", 1) for part in rule.split(";"))
    if "COUNT" not in parts:
        return rule
    recurrence = rrulestr(rule, dtstart=datetime.combine(start_date, time()))
    used = sum(1 for moment in recurrence if moment.date() < boundary)
    remaining = int(parts["COUNT"]) - used
    if remaining <= 0:
        raise validation_error("The series has no occurrences left after from_date")
    parts["COUNT"] = str(remaining)
    return normalize_rule(";".join(f"{key}={value}" for key, value in parts.items()))


def _check_int(parts: dict[str, str], key: str, low: int, high: int) -> None:
    if key not in parts:
        return
    value = parts[key]
    if not value.isdigit() or not low <= int(value) <= high:
        raise validation_error(f"recurrence_rule {key} must be between {low} and {high}")
