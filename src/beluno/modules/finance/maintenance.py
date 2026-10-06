"""Ledger reconciliation and audited balance repair (worker role).

The reconciler recomputes every plan from canonical postings: each transaction
must balance per currency, every account balance must equal the sum of its
postings, and the ledger sequence must have no gaps. Any finding is a
zero-tolerance incident: it is counted, logged with identifiers only, and
repaired with ``rebuild_balances``, which rebuilds the projection from postings
and never edits a posting.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from beluno.modules.context import Runtime, open_context
from beluno.modules.sync_audit.recorder import record_audit
from beluno.observability.metrics import attributes, instruments
from beluno.observability.setup import logger, safe_extra

PLAN_IDS_SQL = text("SELECT finance.ledger_plan_ids(:after, :limit)")
RECONCILE_SQL = text(
    "SELECT problem, account_id, expected_minor, actual_minor FROM finance.reconcile_plan(:plan_id)"
)
REBUILD_SQL = text(
    "SELECT account_id, recorded_minor, rebuilt_minor "
    "FROM finance.rebuild_balances(:plan_id, :apply)"
)
BATCH_SIZE = 200
MAX_BATCHES_PER_RUN = 500

finance_log = logger("beluno.finance")


@dataclass(frozen=True)
class Drift:
    plan_id: UUID
    problem: str
    account_id: UUID | None
    expected_minor: int
    actual_minor: int | None


@dataclass(frozen=True)
class BalanceRepair:
    account_id: UUID
    recorded_minor: int | None
    rebuilt_minor: int


async def reconcile_plan(runtime: Runtime, plan_id: UUID) -> list[Drift]:
    async with runtime.database.transaction() as session:
        rows = (await session.execute(RECONCILE_SQL, {"plan_id": plan_id})).all()
    return [Drift(plan_id, row[0], row[1], row[2], row[3]) for row in rows]


async def reconcile_ledgers(runtime: Runtime) -> int:
    """Check every plan ledger; returns the number of findings (zero when healthy)."""

    meters = instruments()
    findings = 0
    after: UUID | None = None
    for _ in range(MAX_BATCHES_PER_RUN):
        async with runtime.database.transaction() as session:
            plan_ids = list(
                (
                    await session.execute(PLAN_IDS_SQL, {"after": after, "limit": BATCH_SIZE})
                ).scalars()
            )
        if not plan_ids:
            break
        for plan_id in plan_ids:
            drift = await reconcile_plan(runtime, plan_id)
            meters.ledgers_reconciled.add(1)
            for item in drift:
                findings += 1
                meters.ledger_drift.add(1, attributes(problem=item.problem))
                finance_log.error(
                    "ledger drift",
                    **safe_extra(
                        event="ledger_drift",
                        problem=item.problem,
                        plan_id=str(item.plan_id),
                        account_id=str(item.account_id) if item.account_id else None,
                    ),
                )
        after = plan_ids[-1]
    return findings


async def rebuild_balances(
    runtime: Runtime, plan_id: UUID, *, apply: bool, operator: str
) -> list[BalanceRepair]:
    """Diff (and with ``apply`` replace) a plan's balances against its postings; audited."""

    async with open_context(runtime) as ctx:
        rows = (await ctx.session.execute(REBUILD_SQL, {"plan_id": plan_id, "apply": apply})).all()
        repairs = [BalanceRepair(row[0], row[1], row[2]) for row in rows]
        if apply:
            await record_audit(
                ctx,
                action="finance.balances_rebuilt",
                entity_type="ledger",
                entity_id=plan_id,
                metadata={"plan_id": str(plan_id), "accounts": len(repairs), "operator": operator},
            )
    return repairs
