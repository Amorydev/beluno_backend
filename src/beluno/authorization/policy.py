"""Central deny-by-default authorization decisions.

Decisions are pure functions of the caller's *current* relationship state, which
the API loads from PostgreSQL on every request; JWT claims never carry roles.

    decision = actor kind x membership/participant state x role
               x plan/group state x visibility x step-up freshness x action

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


class GroupRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class MembershipState(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"


class GroupState(StrEnum):
    ACTIVE = "active"
    DELETION_SCHEDULED = "deletion_scheduled"


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


class PlanState(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    ACTIVE = "active"
    SETTLING = "settling"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"


class Visibility(StrEnum):
    GROUP = "group"
    PARTICIPANTS = "participants"


class GroupAction(StrEnum):
    VIEW = "group.view"
    UPDATE = "group.update"
    DELETE = "group.delete"
    VIEW_MEMBERS = "group.members.view"
    ADD_MEMBER = "group.members.add"
    REMOVE_MEMBER = "group.members.remove"
    CHANGE_MEMBER_ROLE = "group.members.change_role"
    TRANSFER_OWNERSHIP = "group.ownership.transfer"
    RESPOND_INVITATION = "group.invitation.respond"
    LEAVE = "group.leave"
    CREATE_PLAN = "group.plans.create"


class PlanAction(StrEnum):
    VIEW = "plan.view"
    UPDATE = "plan.update"
    CHANGE_STATE = "plan.state.change"
    DELETE = "plan.delete"
    DUPLICATE = "plan.duplicate"
    JOIN = "plan.join"
    VIEW_PARTICIPANTS = "plan.participants.view"
    ADD_PARTICIPANT = "plan.participants.add"
    REMOVE_PARTICIPANT = "plan.participants.remove"
    CHANGE_PARTICIPANT_ROLE = "plan.participants.change_role"
    REVIEW_JOIN_REQUEST = "plan.participants.review"
    TRANSFER_OWNERSHIP = "plan.ownership.transfer"
    LEAVE = "plan.leave"
    RESPOND_RSVP = "plan.rsvp.respond"
    MANAGE_INVITES = "plan.invites.manage"
    VIEW_TRAVEL = "plan.travel.view"
    MANAGE_TRAVEL = "plan.travel.manage"
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


ALL_PLAN_ROLES = frozenset(PlanRole)
PLAN_MANAGERS = frozenset({PlanRole.OWNER, PlanRole.ADMIN})
PLAN_CONTRIBUTORS = frozenset({PlanRole.OWNER, PlanRole.ADMIN, PlanRole.MEMBER})
# Guests can split costs: the money they spent or owe is theirs to record.
FINANCE_CONTRIBUTORS = PLAN_CONTRIBUTORS | {PlanRole.GUEST}
ALL_GROUP_ROLES = frozenset(GroupRole)
GROUP_MANAGERS = frozenset({GroupRole.OWNER, GroupRole.ADMIN})

# Plan content can change while the plan is being organised or settled.
EDITABLE_PLAN_STATES = frozenset(
    {PlanState.DRAFT, PlanState.PLANNING, PlanState.ACTIVE, PlanState.SETTLING}
)
# Attendance answers only make sense before the plan has happened.
RSVP_PLAN_STATES = frozenset({PlanState.DRAFT, PlanState.PLANNING, PlanState.ACTIVE})
# People still pay each other back after the plan is over.
SETTLEMENT_PLAN_STATES = EDITABLE_PLAN_STATES | {PlanState.COMPLETED}


@dataclass(frozen=True)
class Rule:
    roles: frozenset[str]
    plan_states: frozenset[PlanState] | None = None
    registered_only: bool = False
    step_up: bool = False
    allowed_during_deletion: bool = False


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
    PlanAction.VIEW_TRAVEL: Rule(ALL_PLAN_ROLES, allowed_during_deletion=True),
    PlanAction.MANAGE_TRAVEL: Rule(PLAN_CONTRIBUTORS, EDITABLE_PLAN_STATES),
    PlanAction.VIEW_FINANCE: Rule(ALL_PLAN_ROLES, allowed_during_deletion=True),
    PlanAction.CREATE_EXPENSE: Rule(FINANCE_CONTRIBUTORS, EDITABLE_PLAN_STATES),
    PlanAction.MANAGE_EXPENSES: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    PlanAction.RECORD_SETTLEMENT: Rule(FINANCE_CONTRIBUTORS, SETTLEMENT_PLAN_STATES),
    PlanAction.MANAGE_SETTLEMENTS: Rule(PLAN_MANAGERS, SETTLEMENT_PLAN_STATES),
    # Whoever was paid confirms or disputes it, whatever their role.
    PlanAction.ANSWER_SETTLEMENT: Rule(ALL_PLAN_ROLES, SETTLEMENT_PLAN_STATES),
    PlanAction.MANAGE_BUDGETS: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    PlanAction.CONTRIBUTE_FUND: Rule(FINANCE_CONTRIBUTORS, EDITABLE_PLAN_STATES),
    PlanAction.MANAGE_FUND: Rule(PLAN_MANAGERS, EDITABLE_PLAN_STATES),
    PlanAction.ADJUST_LEDGER: Rule(
        frozenset({PlanRole.OWNER}), EDITABLE_PLAN_STATES, registered_only=True, step_up=True
    ),
}

# Active group members who are not participants of a group-visible plan may read
# it and join it; nothing else.
GROUP_VISIBLE_READ_ACTIONS = frozenset(
    {PlanAction.VIEW, PlanAction.VIEW_PARTICIPANTS, PlanAction.VIEW_TRAVEL}
)

GROUP_RULES: dict[GroupAction, Rule] = {
    GroupAction.VIEW: Rule(ALL_GROUP_ROLES, allowed_during_deletion=True),
    GroupAction.UPDATE: Rule(GROUP_MANAGERS),
    GroupAction.DELETE: Rule(
        frozenset({GroupRole.OWNER}), step_up=True, allowed_during_deletion=True
    ),
    GroupAction.VIEW_MEMBERS: Rule(ALL_GROUP_ROLES, allowed_during_deletion=True),
    GroupAction.ADD_MEMBER: Rule(GROUP_MANAGERS),
    GroupAction.REMOVE_MEMBER: Rule(GROUP_MANAGERS),
    GroupAction.CHANGE_MEMBER_ROLE: Rule(frozenset({GroupRole.OWNER})),
    GroupAction.TRANSFER_OWNERSHIP: Rule(frozenset({GroupRole.OWNER}), step_up=True),
    GroupAction.LEAVE: Rule(ALL_GROUP_ROLES, allowed_during_deletion=True),
    GroupAction.CREATE_PLAN: Rule(ALL_GROUP_ROLES),
}


@dataclass(frozen=True)
class GroupSubject:
    """The caller's current relationship to one group."""

    role: GroupRole | None
    membership_state: MembershipState | None
    group_state: GroupState
    actor_is_guest: bool
    step_up_fresh: bool


@dataclass(frozen=True)
class PlanSubject:
    """The caller's current relationship to one plan."""

    participant_role: PlanRole | None
    access_state: AccessState | None
    group_member_active: bool
    visibility: Visibility
    plan_state: PlanState
    deletion_scheduled: bool
    actor_is_guest: bool
    step_up_fresh: bool


def decide_group(action: GroupAction, subject: GroupSubject) -> Decision:
    state = subject.membership_state
    if state is MembershipState.INVITED:
        if action in (GroupAction.VIEW, GroupAction.RESPOND_INVITATION):
            return Decision.ALLOW
        return Decision.FORBIDDEN
    if state is not MembershipState.ACTIVE or subject.role is None:
        return Decision.HIDDEN
    if action is GroupAction.RESPOND_INVITATION:
        return Decision.FORBIDDEN
    rule = GROUP_RULES[action]
    return _apply_rule(
        rule,
        role=subject.role,
        actor_is_guest=subject.actor_is_guest,
        deletion_scheduled=subject.group_state is GroupState.DELETION_SCHEDULED,
        plan_state=None,
        step_up_fresh=subject.step_up_fresh,
    )


def decide_plan(action: PlanAction, subject: PlanSubject) -> Decision:
    if subject.access_state is AccessState.ACTIVE and subject.participant_role is not None:
        if action is PlanAction.JOIN:
            return Decision.FORBIDDEN
        return _apply_rule(
            PLAN_RULES[action],
            role=subject.participant_role,
            actor_is_guest=subject.actor_is_guest,
            deletion_scheduled=subject.deletion_scheduled,
            plan_state=subject.plan_state,
            step_up_fresh=subject.step_up_fresh,
        )
    if subject.visibility is Visibility.GROUP and subject.group_member_active:
        if action in GROUP_VISIBLE_READ_ACTIONS:
            return Decision.ALLOW
        if action is PlanAction.JOIN:
            joinable = (
                not subject.actor_is_guest
                and not subject.deletion_scheduled
                and subject.plan_state in EDITABLE_PLAN_STATES
                and subject.access_state in (None, AccessState.LEFT)
            )
            return Decision.ALLOW if joinable else Decision.FORBIDDEN
        return Decision.FORBIDDEN
    return Decision.HIDDEN


def _apply_rule(
    rule: Rule,
    *,
    role: str,
    actor_is_guest: bool,
    deletion_scheduled: bool,
    plan_state: PlanState | None,
    step_up_fresh: bool,
) -> Decision:
    if role not in rule.roles:
        return Decision.FORBIDDEN
    if rule.registered_only and actor_is_guest:
        return Decision.FORBIDDEN
    if deletion_scheduled and not rule.allowed_during_deletion:
        return Decision.FORBIDDEN
    if rule.plan_states is not None and plan_state not in rule.plan_states:
        return Decision.FORBIDDEN
    if rule.step_up and not step_up_fresh:
        return Decision.STEP_UP_REQUIRED
    return Decision.ALLOW


def can_manage_group_member(
    actor_role: GroupRole,
    target_role: GroupRole,
    *,
    new_role: GroupRole | None = None,
) -> bool:
    """Owners manage everyone but themselves; admins manage plain members only."""

    if target_role is GroupRole.OWNER or new_role is GroupRole.OWNER:
        return False
    if actor_role is GroupRole.OWNER:
        return True
    if actor_role is GroupRole.ADMIN:
        return target_role is GroupRole.MEMBER and new_role in (None, GroupRole.MEMBER)
    return False


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
