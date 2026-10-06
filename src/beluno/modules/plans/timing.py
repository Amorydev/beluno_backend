"""Plan timing rules: date-only vs timed values, IANA zones, DST, and lifecycle states.

Timed values are stored as UTC instants plus the IANA zone they were planned in;
date-only values stay SQL ``date`` and are never faked as midnight UTC.

Local wall-clock resolution (``resolve_local``):
* ambiguous times (clock set back) resolve to the earlier instant;
* non-existent times (clock jumps forward) are rejected for one-off inputs such
  as travel segments, and shifted forward by the gap for recurring occurrences,
  so a weekly 02:30 match still happens on the night the clocks change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from beluno.authorization.policy import PlanState
from beluno.contracts.errors import invalid_state, validation_error


class GapPolicy(StrEnum):
    REJECT = "reject"
    SHIFT_FORWARD = "shift_forward"


def resolve_local(local: datetime, zone_name: str, *, on_gap: GapPolicy) -> datetime:
    """Return the UTC instant for a naive local wall-clock time in ``zone_name``."""

    if local.tzinfo is not None:
        raise ValueError("local time must be naive")
    zone = ZoneInfo(zone_name)
    instant = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
    round_trip = instant.astimezone(zone).replace(tzinfo=None)
    if round_trip != local and on_gap is GapPolicy.REJECT:
        raise validation_error("This local time does not exist in that timezone (DST change)")
    return instant


def require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise validation_error(f"{field} must include a UTC offset")
    return value.astimezone(UTC)


def check_date_range(start: date, end: date | None) -> None:
    if end is not None and end < start:
        raise validation_error("end date must not be before start date")


def check_instant_range(start: datetime, end: datetime | None) -> None:
    if end is not None and end < start:
        raise validation_error("end must not be before start")


# Reopen paths exist so a mistaken completion or cancellation can be undone.
ALLOWED_TRANSITIONS: dict[PlanState, frozenset[PlanState]] = {
    PlanState.DRAFT: frozenset({PlanState.PLANNING, PlanState.ACTIVE, PlanState.CANCELLED}),
    PlanState.PLANNING: frozenset({PlanState.DRAFT, PlanState.ACTIVE, PlanState.CANCELLED}),
    PlanState.ACTIVE: frozenset(
        {PlanState.PLANNING, PlanState.SETTLING, PlanState.COMPLETED, PlanState.CANCELLED}
    ),
    PlanState.SETTLING: frozenset({PlanState.ACTIVE, PlanState.COMPLETED}),
    PlanState.COMPLETED: frozenset({PlanState.SETTLING, PlanState.ARCHIVED}),
    PlanState.CANCELLED: frozenset({PlanState.PLANNING, PlanState.ARCHIVED}),
    PlanState.ARCHIVED: frozenset({PlanState.COMPLETED}),
}


def check_transition(current: PlanState, target: PlanState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise invalid_state(f"A {current.value} plan cannot become {target.value}")
