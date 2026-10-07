"""Activity event types and their summaries.

An event says what happened ("An added an expense", "Bea left") with IDs and a
small typed summary: amounts and currencies, roles, states, dates, field names,
and before/after values. Free text never enters a summary (descriptions, notes,
names, codes, addresses); clients show it by joining the IDs with synced
entities. ``ActivityItem`` refuses any key outside the allowed set, so a new
event type cannot leak text by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

ACTIVITY_ENTITY = "activity_event"


class ActivityType(StrEnum):
    EXPENSE_ADDED = "expense.added"
    EXPENSE_EDITED = "expense.edited"
    EXPENSE_REFUNDED = "expense.refunded"
    EXPENSE_VOIDED = "expense.voided"
    PAYMENT_RECORDED = "payment.recorded"
    PAYMENT_REVERSED = "payment.reversed"
    WAIVER_GIVEN = "waiver.given"
    BUDGET_CHANGED = "budget.changed"
    BASE_CURRENCY_CHANGED = "base_currency.changed"
    LEDGER_CONSOLIDATED = "ledger.consolidated"
    CONSOLIDATION_REVERSED = "ledger.consolidation_reversed"
    KITTY_CONTRIBUTED = "kitty.contributed"
    KITTY_WITHDRAWN = "kitty.withdrawn"
    KITTY_COUNTED = "kitty.counted"
    MEMBER_JOINED = "member.joined"
    MEMBER_LEFT = "member.left"
    MEMBER_REMOVED = "member.removed"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_CAPABILITIES_CHANGED = "member.capabilities_changed"
    GUEST_LINKED = "guest.linked"
    PLAN_CREATED = "plan.created"
    PLAN_DATES_CHANGED = "plan.dates_changed"
    PLAN_STATE_CHANGED = "plan.state_changed"
    ACCOUNT_GUEST_UPGRADED = "account.guest_upgraded"
    ACCOUNT_GUEST_MERGED = "account.guest_merged"


# Every key a summary may carry; values are numbers, booleans, IDs, codes from a
# closed set (currency, role, state, category, field name), or ISO dates.
SUMMARY_KEYS = frozenset(
    {
        "amount_minor",
        "previous_amount_minor",
        "currency",
        "previous_currency",
        "base_currency",
        "from_currency",
        "to_currency",
        "currencies",
        "category",
        "fields",
        "participant_id",
        "from_participant_id",
        "to_participant_id",
        "guest_participant_id",
        "guest_user_id",
        "role",
        "previous_role",
        "capabilities",
        "previous_capabilities",
        "state",
        "previous_state",
        "type",
        "scope",
        "operation",
        "limit_minor",
        "previous_limit_minor",
        "counted_minor",
        "expected_minor",
        "difference_minor",
        "timing_mode",
        "start_date",
        "end_date",
        "previous_start_date",
        "previous_end_date",
    }
)

SummaryValue = int | str | bool | None | Sequence[str]


@dataclass(frozen=True)
class ActivityItem:
    type: ActivityType
    summary: Mapping[str, SummaryValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.summary) - SUMMARY_KEYS
        if unknown:
            raise ValueError(f"activity summary keys not allowed: {sorted(unknown)}")
        for value in self.summary.values():
            if isinstance(value, UUID):
                raise TypeError("pass IDs to activity summaries as strings")


def item(kind: ActivityType, **summary: SummaryValue | UUID) -> ActivityItem:
    """An activity item; UUID values become strings."""

    return ActivityItem(
        kind,
        {key: str(value) if isinstance(value, UUID) else value for key, value in summary.items()},
    )
