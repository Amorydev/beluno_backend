"""Central deny-by-default authorization decisions.

Decisions are pure functions of the caller's *current* relationship state, which
the API loads from PostgreSQL on every request; JWT claims never carry roles.

    decision = actor kind x participant state x role x plan state
               x step-up freshness x action

A caller with no viewing relationship gets ``HIDDEN`` (rendered as 404) so the
response never confirms that a resource exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Decision(StrEnum):
    ALLOW = "allow"
    HIDDEN = "hidden"
    FORBIDDEN = "forbidden"
    STEP_UP_REQUIRED = "step_up_required"


class PlanRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    GUEST = "guest"


class AccessState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"
    MERGED = "merged"


class Capability(StrEnum):
    """Per-member grants on top of the role ("Edit others' expenses: Admins + Quân")."""

    MANAGE_EXPENSES = "expenses.manage"
    MANAGE_BUDGETS = "budgets.manage"


class PlanState(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    ACTIVE = "active"
    SETTLING = "settling"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"


class PlanAction(StrEnum):
    VIEW = "plan.view"
    UPDATE = "plan.update"
    CHANGE_STATE = "plan.state.change"
    DELETE = "plan.delete"
    DUPLICATE = "plan.duplicate"
    VIEW_PARTICIPANTS = "plan.participants.view"
    ADD_PARTICIPANT = "plan.participants.add"
    REMOVE_PARTICIPANT = "plan.participants.remove"
    CHANGE_PARTICIPANT_ROLE = "plan.participants.change_role"
    REVIEW_JOIN_REQUEST = "plan.participants.review"
    TRANSFER_OWNERSHIP = "plan.ownership.transfer"
    LEAVE = "plan.leave"
    RESPOND_RSVP = "plan.rsvp.respond"
    MANAGE_INVITES = "plan.invites.manage"
    VIEW_FINANCE = "plan.finance.view"
    CREATE_EXPENSE = "plan.expenses.create"
    MANAGE_EXPENSES = "plan.expenses.manage"
    RECORD_SETTLEMENT = "plan.settlements.record"
    MANAGE_SETTLEMENTS = "plan.settlements.manage"
    ANSWER_SETTLEMENT = "plan.settlements.answer"
    MANAGE_BUDGETS = "plan.budgets.manage"
    CONTRIBUTE_FUND = "plan.fund.contribute"
    MANAGE_FUND = "plan.fund.manage"
    ADJUST_LEDGER = "plan.ledger.adjust"
    CONFIGURE_LEDGER = "plan.ledger.configure"
    CONFIRM_LEDGER = "plan.ledger.confirm"
    CONSOLIDATE_LEDGER = "plan.ledger.consolidate"
    CHANGE_BASE_CURRENCY = "plan.base_currency.change"
    CONTRIBUTE_PLANNING = "plan.planning.contribute"
    MANAGE_PLANNING = "plan.planning.manage"
    RESPOND_PLANNING = "plan.planning.respond"


ALL_PLAN_ROLES = frozenset(PlanRole)
PLAN_MANAGERS = frozenset({PlanRole.OWNER, PlanRole.ADMIN})
PLAN_CONTRIBUTORS = frozenset({PlanRole.OWNER, PlanRole.ADMIN, PlanRole.MEMBER})
# Guests can split costs: the money they spent or owe is theirs to record.
FINANCE_CONTRIBUTORS = PLAN_CONTRIBUTORS | {PlanRole.GUEST}

# Plan content can change while the plan is being organised or settled.
EDITABLE_PLAN_STATES = frozenset(
    {PlanState.DRAFT, PlanState.PLANNING, PlanState.ACTIVE, PlanState.SETTLING}
)
# Attendance answers only make sense before the plan has happened.
RSVP_PLAN_STATES = frozenset({PlanState.DRAFT, PlanState.PLANNING, PlanState.ACTIVE})
# People still pay each other back after the plan is over.
SETTLEMENT_PLAN_STATES = EDITABLE_PLAN_STATES | {PlanState.COMPLETED}


# Only plain members hold capabilities; managers already have them and viewers and
# guests cannot be granted management.
CAPABILITY_ROLES = frozenset({PlanRole.MEMBER})


@dataclass(frozen=True)
class Rule:
    roles: frozenset[str]
    plan_states: frozenset[PlanState] | None = None
    registered_only: bool = False
    step_up: bool = False
    allowed_during_deletion: bool = False
    # A member holding this capability passes as if their role were listed.
    capability: Capability | None = None


PLAN_RULES: dict[PlanAction, Rule] = {
    PlanAction.VIEW: Rule(ALL_PLAN_ROLES, allowed_during_deletion=True),
    PlanAction.UPDATE: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    PlanAction.CHANGE_STATE: Rule(PLAN_MANAGERS),
    PlanAction.DELETE: Rule(
        frozenset({PlanRole.OWNER}),
        registered_only=True,
        step_up=True,
        allowed_during_deletion=True,
    ),
    PlanAction.DUPLICATE: Rule(PLAN_MANAGERS, registered_only=True),
    PlanAction.VIEW_PARTICIPANTS: Rule(ALL_PLAN_ROLES, allowed_during_deletion=True),
    PlanAction.ADD_PARTICIPANT: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES, registered_only=True),
    PlanAction.REMOVE_PARTICIPANT: Rule(PLAN_MANAGERS, registered_only=True),
    PlanAction.CHANGE_PARTICIPANT_ROLE: Rule(PLAN_MANAGERS, registered_only=True),
    PlanAction.REVIEW_JOIN_REQUEST: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES, registered_only=True),
    PlanAction.TRANSFER_OWNERSHIP: Rule(
        frozenset({PlanRole.OWNER}), registered_only=True, step_up=True
    ),
    PlanAction.LEAVE: Rule(ALL_PLAN_ROLES, allowed_during_deletion=True),
    PlanAction.RESPOND_RSVP: Rule(ALL_PLAN_ROLES, RSVP_PLAN_STATES),
    PlanAction.MANAGE_INVITES: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES, registered_only=True),
    PlanAction.VIEW_FINANCE: Rule(ALL_PLAN_ROLES, allowed_during_deletion=True),
    PlanAction.CREATE_EXPENSE: Rule(FINANCE_CONTRIBUTORS, EDITABLE_PLAN_STATES),
    PlanAction.MANAGE_EXPENSES: Rule(
        PLAN_MANAGERS, EDITABLE_PLAN_STATES, capability=Capability.MANAGE_EXPENSES
    ),
    PlanAction.RECORD_SETTLEMENT: Rule(FINANCE_CONTRIBUTORS, SETTLEMENT_PLAN_STATES),
    PlanAction.MANAGE_SETTLEMENTS: Rule(PLAN_MANAGERS, SETTLEMENT_PLAN_STATES),
    # Whoever was paid confirms or disputes it, whatever their role.
    PlanAction.ANSWER_SETTLEMENT: Rule(ALL_PLAN_ROLES, SETTLEMENT_PLAN_STATES),
    PlanAction.MANAGE_BUDGETS: Rule(
        PLAN_MANAGERS, EDITABLE_PLAN_STATES, capability=Capability.MANAGE_BUDGETS
    ),
    PlanAction.CONTRIBUTE_FUND: Rule(FINANCE_CONTRIBUTORS, EDITABLE_PLAN_STATES),
    PlanAction.MANAGE_FUND: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    PlanAction.ADJUST_LEDGER: Rule(
        frozenset({PlanRole.OWNER}), EDITABLE_PLAN_STATES, registered_only=True, step_up=True
    ),
    # Money settings of the plan (count personal spend, the settled-under tolerance).
    PlanAction.CONFIGURE_LEDGER: Rule(PLAN_MANAGERS, SETTLEMENT_PLAN_STATES),
    # Everyone may say the ledger looks right to them; it never blocks anything.
    PlanAction.CONFIRM_LEDGER: Rule(ALL_PLAN_ROLES, SETTLEMENT_PLAN_STATES),
    # Converting every balance into the base currency changes what everyone owes.
    PlanAction.CONSOLIDATE_LEDGER: Rule(PLAN_MANAGERS, SETTLEMENT_PLAN_STATES),
    # It re-denominates budgets and every base-currency value everyone sees.
    PlanAction.CHANGE_BASE_CURRENCY: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    # Places, itinerary items, and the rest of the trip plan: anyone taking part adds
    # them (guests too); managers change anyone's; viewers only read and respond.
    PlanAction.CONTRIBUTE_PLANNING: Rule(FINANCE_CONTRIBUTORS, EDITABLE_PLAN_STATES),
    PlanAction.MANAGE_PLANNING: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    # "Want to go", "going / not going": every participant answers for themselves.
    PlanAction.RESPOND_PLANNING: Rule(ALL_PLAN_ROLES, EDITABLE_PLAN_STATES),
}


@dataclass(frozen=True)
class PlanSubject:
    """The caller's current relationship to one plan."""

    participant_role: PlanRole | None
    access_state: AccessState | None
    plan_state: PlanState
    deletion_scheduled: bool
    actor_is_guest: bool
    step_up_fresh: bool
    capabilities: frozenset[Capability] = frozenset()


def decide_plan(action: PlanAction, subject: PlanSubject) -> Decision:
    if subject.access_state is not AccessState.ACTIVE or subject.participant_role is None:
        return Decision.HIDDEN
    rule = PLAN_RULES[action]
    role = subject.participant_role
    granted = (
        rule.capability is not None
        and rule.capability in subject.capabilities
        and role in CAPABILITY_ROLES
    )
    if role not in rule.roles and not granted:
        return Decision.FORBIDDEN
    if rule.registered_only and subject.actor_is_guest:
        return Decision.FORBIDDEN
    if subject.deletion_scheduled and not rule.allowed_during_deletion:
        return Decision.FORBIDDEN
    if rule.plan_states is not None and subject.plan_state not in rule.plan_states:
        return Decision.FORBIDDEN
    if rule.step_up and not subject.step_up_fresh:
        return Decision.STEP_UP_REQUIRED
    return Decision.ALLOW


def can_manage_participant(
    actor_role: PlanRole,
    target_role: PlanRole,
    *,
    new_role: PlanRole | None = None,
) -> bool:
    """Owners manage every non-owner; admins manage members, viewers, and guests.

    Ownership only moves through the transfer command, and the guest role is tied
    to guest identities, so neither can be assigned through a role change.
    """

    if target_role is PlanRole.OWNER or new_role in (PlanRole.OWNER, PlanRole.GUEST):
        return False
    if new_role is not None and target_role is PlanRole.GUEST:
        return False
    if actor_role is PlanRole.OWNER:
        return True
    if actor_role is PlanRole.ADMIN:
        manageable = (PlanRole.MEMBER, PlanRole.VIEWER, PlanRole.GUEST)
        return target_role in manageable and new_role in (None, PlanRole.MEMBER, PlanRole.VIEWER)
    return False
