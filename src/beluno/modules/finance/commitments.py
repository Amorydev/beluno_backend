"""Cost commitments: planned or committed spending that may later become an expense.

Finance owns these rows. Other modules (bookings, itinerary items, places,
responsibilities) create and change them only through ``CostCommitmentPort``,
after their own authorization; REST exposes ``manual`` commitments for budget
planning. Linking an expense converts the commitment in the same transaction,
so budgets count each source exactly once: actual, else committed, else estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess, load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import (
    conflict,
    forbidden,
    invalid_state,
    not_found,
    validation_error,
    version_conflict,
)
from beluno.db.ids import new_id
from beluno.db.models.finance import CostCommitment, Expense, FxSnapshot
from beluno.db.models.plans import Plan
from beluno.modules.context import CommandContext
from beluno.modules.finance.base_currency import base_currency_at
from beluno.modules.finance.fx import RateSource, convert, parse_rate
from beluno.modules.finance.ledger import Ledger, ledger_for, open_ledger
from beluno.modules.finance.money import check_amount
from beluno.modules.finance.rates import RateInput, record_rate
from beluno.modules.finance.states import (
    COMMITMENT_TRANSITIONS,
    LINKABLE_COMMITMENT_STATES,
    CommitmentState,
)
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation

COMMITMENT_ENTITY = "cost_commitment"
MANUAL = "manual"
SOURCE_TYPES = frozenset({MANUAL, "booking", "itinerary_item", "place", "responsibility"})


@dataclass(frozen=True)
class CommitmentDraft:
    category: str
    description: str
    currency: str
    amount_minor: int
    state: CommitmentState
    base_rate: RateInput | None = None


@dataclass(frozen=True)
class CommitmentView:
    commitment: CostCommitment
    rate: FxSnapshot | None
    base_currency: str


# --- reads ---------------------------------------------------------------------------


async def list_commitments(ctx: CommandContext, plan_id: UUID) -> list[CommitmentView]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    rows = await ctx.session.execute(
        select(CostCommitment).where(CostCommitment.plan_id == plan_id).order_by(CostCommitment.id)
    )
    return [await commitment_view(ctx, row) for row in rows.scalars()]


async def commitment_view(ctx: CommandContext, commitment: CostCommitment) -> CommitmentView:
    rate = (
        await ctx.session.get(FxSnapshot, commitment.base_fx_snapshot_id)
        if commitment.base_fx_snapshot_id
        else None
    )
    plan = await ctx.session.get(Plan, commitment.plan_id)
    assert plan is not None
    # The base amount is in the base currency of the time it was valued.
    base = await base_currency_at(ctx, plan.id, commitment.base_change_number, plan.base_currency)
    return CommitmentView(commitment=commitment, rate=rate, base_currency=base)


# --- manual commitments (REST) --------------------------------------------------------


async def create_manual(
    ctx: CommandContext, plan_id: UUID, commitment_id: UUID | None, draft: CommitmentDraft
) -> CommitmentView:
    ledger = await open_ledger(ctx, plan_id, PlanAction.MANAGE_BUDGETS)
    ledger.require_trip()
    if draft.state not in LINKABLE_COMMITMENT_STATES:
        raise validation_error("a new commitment is estimated or committed")
    identity = commitment_id or new_id()
    commitment = await _create(ledger, MANUAL, identity, MANUAL, draft, commitment_id=identity)
    return await commitment_view(ctx, commitment)


async def update_manual(
    ctx: CommandContext,
    plan_id: UUID,
    commitment_id: UUID,
    draft: CommitmentDraft,
    expected_version: int,
) -> CommitmentView:
    ledger = await open_ledger(ctx, plan_id, PlanAction.MANAGE_BUDGETS)
    ledger.require_trip()
    commitment = await _locked(ctx, plan_id, commitment_id)
    if commitment.source_type != MANUAL:
        raise forbidden("Change this cost where it was planned")
    if commitment.version != expected_version:
        raise version_conflict(commitment)
    current = CommitmentState(commitment.state)
    if draft.state is not current and draft.state not in COMMITMENT_TRANSITIONS[current]:
        raise invalid_state(f"A {current.value} commitment cannot become {draft.state.value}")
    await _apply(ledger, commitment, draft)
    commitment.state = draft.state.value
    await _bump(ledger, commitment, "finance.commitment_updated")
    return await commitment_view(ctx, commitment)


# --- the port --------------------------------------------------------------------------


class CostCommitmentPort:
    """How other modules record the costs they plan; finance tables stay finance's.

    Callers pass the plan access they already authorized for their own action,
    loaded with the plan row locked (``load_plan(..., for_update=True)``), and run
    inside their own transaction, so the commitment commits with the booking,
    itinerary item, or task that caused it.
    """

    async def record(
        self,
        ctx: CommandContext,
        access: PlanAccess,
        *,
        source_type: str,
        source_id: UUID,
        commitment_kind: str,
        draft: CommitmentDraft,
    ) -> CostCommitment:
        """Create or update the commitment for one source; linked ones keep their state."""

        if source_type not in SOURCE_TYPES or source_type == MANUAL:
            raise ValueError(f"unsupported commitment source {source_type!r}")
        if draft.state not in LINKABLE_COMMITMENT_STATES:
            raise ValueError("ports record estimated or committed costs; use cancel()")
        ledger = await ledger_for(ctx, access)
        ledger.require_trip()
        existing = await _by_source(ctx, access.plan.id, source_type, source_id, commitment_kind)
        if existing is None:
            return await _create(ledger, source_type, source_id, commitment_kind, draft)
        await _apply(ledger, existing, draft)
        # A cost that already became an expense keeps the state the expense gave it.
        if CommitmentState(existing.state) not in (
            CommitmentState.CONVERTED,
            CommitmentState.REFUNDED,
        ):
            existing.state = draft.state.value
        await _bump(ledger, existing, "finance.commitment_updated")
        return existing

    async def cancel(
        self,
        ctx: CommandContext,
        access: PlanAccess,
        *,
        source_type: str,
        source_id: UUID,
        commitment_kind: str,
    ) -> None:
        ledger = await ledger_for(ctx, access)
        existing = await _by_source(ctx, access.plan.id, source_type, source_id, commitment_kind)
        if existing is None or existing.state == CommitmentState.CANCELLED.value:
            return
        if existing.state not in {s.value for s in LINKABLE_COMMITMENT_STATES}:
            raise invalid_state("A cost that became an expense is changed through the expense")
        existing.state = CommitmentState.CANCELLED.value
        await _bump(ledger, existing, "finance.commitment_cancelled")


COST_COMMITMENTS = CostCommitmentPort()


# --- expense links (used by expenses) ------------------------------------------------


async def link_expense(ledger: Ledger, commitment_id: UUID, expense: Expense) -> None:
    commitment = await _find(ledger.ctx, ledger.plan_id, commitment_id, for_update=True)
    if commitment is None:
        raise validation_error("commitment_id does not name a cost commitment of this plan")
    if commitment.expense_id == expense.id:
        return
    state = CommitmentState(commitment.state)
    if state not in LINKABLE_COMMITMENT_STATES:
        raise invalid_state("This cost commitment is already an expense or was cancelled")
    commitment.converted_from_state = state.value
    commitment.state = CommitmentState.CONVERTED.value
    commitment.expense_id = expense.id
    await _bump(ledger, commitment, "finance.commitment_converted")


async def release_expense(ledger: Ledger, commitment_id: UUID, expense_id: UUID) -> None:
    """The expense no longer accounts for this cost: restore its earlier tier."""

    commitment = await _find(ledger.ctx, ledger.plan_id, commitment_id, for_update=True)
    if commitment is None or commitment.expense_id != expense_id:
        return
    commitment.state = commitment.converted_from_state or CommitmentState.COMMITTED.value
    commitment.converted_from_state = None
    commitment.expense_id = None
    await _bump(ledger, commitment, "finance.commitment_released")


# --- helpers -------------------------------------------------------------------------


async def _create(
    ledger: Ledger,
    source_type: str,
    source_id: UUID,
    commitment_kind: str,
    draft: CommitmentDraft,
    *,
    commitment_id: UUID | None = None,
) -> CostCommitment:
    ctx = ledger.ctx
    commitment = CostCommitment(
        id=commitment_id or new_id(),
        plan_id=ledger.plan_id,
        source_type=source_type,
        source_id=source_id,
        commitment_kind=commitment_kind,
        state=draft.state.value,
        converted_from_state=None,
        expense_id=None,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    await _apply(ledger, commitment, draft)
    try:
        async with ctx.savepoint():
            ctx.session.add(commitment)
            await ctx.session.flush()
    except IntegrityError as error:
        raise conflict("ALREADY_EXISTS", "A resource with this id already exists") from error
    await _record(ctx, commitment, "finance.commitment_created")
    return commitment


async def _apply(ledger: Ledger, commitment: CostCommitment, draft: CommitmentDraft) -> None:
    check_amount(draft.amount_minor, field="amount_minor")
    currency = await ledger.currency(draft.currency)
    base_currency = ledger.access.plan.base_currency
    snapshot_id: UUID | None = None
    base_amount: int | None = None
    if draft.currency == base_currency:
        base_amount = draft.amount_minor
    elif draft.base_rate is not None:
        base = await ledger.currency(base_currency)
        rate = parse_rate(draft.base_rate.rate)
        # Record the snapshot before touching the row: its flush must not write a
        # half-changed commitment.
        snapshot = await record_rate(
            ledger,
            base=draft.currency,
            quote=base_currency,
            rate=rate,
            source=RateSource(draft.base_rate.source),
            as_of=draft.base_rate.as_of,
        )
        snapshot_id = snapshot.id
        base_amount = convert(
            draft.amount_minor,
            from_exponent=currency.exponent,
            to_exponent=base.exponent,
            rate=rate,
        )
    commitment.category = draft.category
    commitment.description = draft.description
    commitment.currency = draft.currency
    commitment.amount_minor = draft.amount_minor
    commitment.base_fx_snapshot_id = snapshot_id
    commitment.base_amount_minor = base_amount
    commitment.base_change_number = ledger.head.base_change_count


async def _bump(ledger: Ledger, commitment: CostCommitment, action: str) -> None:
    commitment.version += 1
    commitment.updated_at = ledger.ctx.now
    await ledger.ctx.session.flush()
    await _record(ledger.ctx, commitment, action)


async def _find(
    ctx: CommandContext, plan_id: UUID, commitment_id: UUID, *, for_update: bool = False
) -> CostCommitment | None:
    statement = select(CostCommitment).where(
        CostCommitment.plan_id == plan_id, CostCommitment.id == commitment_id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def _locked(ctx: CommandContext, plan_id: UUID, commitment_id: UUID) -> CostCommitment:
    commitment = await _find(ctx, plan_id, commitment_id, for_update=True)
    if commitment is None:
        raise not_found()
    return commitment


async def _by_source(
    ctx: CommandContext, plan_id: UUID, source_type: str, source_id: UUID, kind: str
) -> CostCommitment | None:
    statement = (
        select(CostCommitment)
        .where(
            CostCommitment.plan_id == plan_id,
            CostCommitment.source_type == source_type,
            CostCommitment.source_id == source_id,
            CostCommitment.commitment_kind == kind,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def _record(ctx: CommandContext, commitment: CostCommitment, action: str) -> None:
    await record_mutation(
        ctx,
        action=action,
        entity_type=COMMITMENT_ENTITY,
        entity_id=commitment.id,
        entity_version=commitment.version,
        scope=ChangeScope.PLAN,
        scope_id=commitment.plan_id,
        plan_id=commitment.plan_id,
        metadata={"version": commitment.version, "state": commitment.state},
    )
