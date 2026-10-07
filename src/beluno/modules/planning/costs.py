"""Planned costs of trip-plan entries (itinerary items, bookings) as finance commitments.

Each entry has at most one commitment per kind, recorded through
``CostCommitmentPort``. A save writes it only when something changed (amount,
currency, category, description, or tier), and only then counts as a finance
write under the finance kill switch. Withdrawing a cost cancels it while it is
still planned; one an expense already paid stays, remembering the withdrawal.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.access import PlanAccess
from beluno.contracts.errors import feature_disabled
from beluno.db.models.finance import CostCommitment
from beluno.modules.context import CommandContext
from beluno.modules.finance.commitments import COST_COMMITMENTS, CommitmentDraft
from beluno.modules.finance.states import LINKABLE_COMMITMENT_STATES, CommitmentState

LINKABLE = frozenset(state.value for state in LINKABLE_COMMITMENT_STATES)


@dataclass(frozen=True)
class PlannedCost:
    currency: str
    amount_minor: int
    category: str | None = None  # None keeps the current one (or the source's default)


@dataclass(frozen=True)
class CostSource:
    source_type: str
    source_id: UUID
    kind: str
    description: str
    default_category: str


async def live_cost(
    ctx: CommandContext, plan_id: UUID, source: CostSource
) -> CostCommitment | None:
    return await ctx.session.scalar(
        select(CostCommitment).where(
            CostCommitment.plan_id == plan_id,
            CostCommitment.source_type == source.source_type,
            CostCommitment.source_id == source.source_id,
            CostCommitment.commitment_kind == source.kind,
            CostCommitment.state != CommitmentState.CANCELLED.value,
        )
    )


async def sync_cost(
    ctx: CommandContext,
    access: PlanAccess,
    source: CostSource,
    cost: PlannedCost | None,
    *,
    tier: CommitmentState = CommitmentState.ESTIMATED,
) -> UUID | None:
    """Bring the source's commitment in line with ``cost`` at ``tier``; return its ID."""

    current = await live_cost(ctx, access.plan.id, source)
    if cost is None:
        withdrawn = current is not None and (
            current.state in LINKABLE
            or current.converted_from_state != CommitmentState.CANCELLED.value
        )
        if withdrawn:
            _require_finance_writes(ctx)
            await COST_COMMITMENTS.cancel(
                ctx,
                access,
                source_type=source.source_type,
                source_id=source.source_id,
                commitment_kind=source.kind,
            )
        return None
    category = cost.category or (current.category if current else source.default_category)
    if (
        current is not None
        and (
            current.currency,
            current.amount_minor,
            current.category,
            current.description,
        )
        == (cost.currency, cost.amount_minor, category, source.description)
        # A paid cost is unchanged when it remembers this tier (what voiding restores).
        and (current.state if current.state in LINKABLE else current.converted_from_state)
        == tier.value
    ):
        return current.id
    _require_finance_writes(ctx)
    commitment = await COST_COMMITMENTS.record(
        ctx,
        access,
        source_type=source.source_type,
        source_id=source.source_id,
        commitment_kind=source.kind,
        draft=CommitmentDraft(
            category=category,
            description=source.description,
            currency=cost.currency,
            amount_minor=cost.amount_minor,
            state=tier,
        ),
    )
    return commitment.id


async def live_costs(
    ctx: CommandContext, plan_id: UUID, source_type: str, kind: str, source_ids: list[UUID]
) -> dict[UUID, UUID]:
    """Source ID -> its commitment ID, for costs that were not cancelled."""

    return await COST_COMMITMENTS.live_for_sources(
        ctx, plan_id, source_type=source_type, source_ids=source_ids, commitment_kind=kind
    )


def _require_finance_writes(ctx: CommandContext) -> None:
    """A cost commitment is a finance write: the finance kill switch stops it too."""

    if not ctx.settings.finance_writes_enabled:
        raise feature_disabled()
