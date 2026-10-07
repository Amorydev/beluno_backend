"""Problem reports and the checks behind "Report a problem".

A report keeps the person's own words, a category, and optionally the plan and
record it is about. With diagnostics attached it also keeps a snapshot an
operator can read without seeing anyone's data: identifiers, states, versions,
sequence numbers, and whether the plan's ledger reconciles. Never expense text,
notes, names, or booking codes. The snapshot's code (``BLN-XXXX-XXXX``) is what
the person quotes to support.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from beluno.authorization.access import PlanAccess, load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import validation_error
from beluno.db.ids import new_id
from beluno.db.models.bookings import Booking
from beluno.db.models.coordination import PackingItem, Task
from beluno.db.models.decisions import Poll
from beluno.db.models.finance import Budget, Expense, LedgerHead, Settlement
from beluno.db.models.iam import AuthSession
from beluno.db.models.schedule_places import ItineraryItem, Place
from beluno.db.models.support import ProblemReport
from beluno.db.models.sync_audit import ScopeHead
from beluno.modules.context import CommandContext, Runtime, open_context
from beluno.modules.sync_audit.recorder import ChangeScope, record_audit

# Crockford base32: no I, L, O, or U to misread.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_ATTEMPTS = 3
LEDGER_PROBLEMS = text("SELECT finance.actor_ledger_problems(:plan_id)")
FORGET_REPORTS = text("SELECT analytics_ops.forget_problem_reports()")

RECORD_MODELS: dict[str, type[Any]] = {
    "expense": Expense,
    "settlement": Settlement,
    "budget": Budget,
    "place": Place,
    "itinerary_item": ItineraryItem,
    "poll": Poll,
    "booking": Booking,
    "task": Task,
    "packing_item": PackingItem,
}


@dataclass(frozen=True)
class Linked:
    entity_type: str
    entity_id: UUID


@dataclass(frozen=True)
class ReportDraft:
    category: str
    message: str
    plan_id: UUID | None = None
    linked: Linked | None = None
    attach_diagnostics: bool = True
    client: dict[str, Any] | None = None


@dataclass(frozen=True)
class RecordCheck:
    plan_id: UUID
    ledger_ok: bool
    record_found: bool | None
    record_version: int | None


async def check_record(ctx: CommandContext, plan_id: UUID, linked: Linked | None) -> RecordCheck:
    """Whether the plan's ledger reconciles, and whether the record is in the plan."""

    await _plan(ctx, plan_id)
    record = await _record(ctx, plan_id, linked) if linked is not None else None
    return RecordCheck(
        plan_id=plan_id,
        ledger_ok=await _ledger_problems(ctx, plan_id) == 0,
        record_found=None if linked is None else record is not None,
        record_version=getattr(record, "version", None),
    )


async def report_problem(ctx: CommandContext, draft: ReportDraft) -> ProblemReport:
    access = await _plan(ctx, draft.plan_id) if draft.plan_id is not None else None
    record = None
    if draft.linked is not None:
        assert draft.plan_id is not None  # the contract requires it
        record = await _record(ctx, draft.plan_id, draft.linked)
        if record is None:
            raise validation_error("the linked record is not in this plan")
    diagnostics = (
        await _diagnostics(ctx, draft, access, record) if draft.attach_diagnostics else None
    )
    for attempt in range(CODE_ATTEMPTS):
        report = ProblemReport(
            id=new_id(),
            user_id=ctx.require_actor().user_id,
            category=draft.category,
            plan_id=draft.plan_id,
            entity_type=draft.linked.entity_type if draft.linked else None,
            entity_id=draft.linked.entity_id if draft.linked else None,
            message=draft.message,
            diagnostic_code=new_code() if diagnostics is not None else None,
            diagnostics=diagnostics,
            created_at=ctx.now,
        )
        try:
            async with ctx.savepoint():
                ctx.session.add(report)
                await ctx.session.flush()
            break
        except IntegrityError as error:
            # A diagnostic code drawn twice: draw again. Anything else is a bug.
            if not isinstance(error.orig, UniqueViolation) or attempt == CODE_ATTEMPTS - 1:
                raise
    await record_audit(
        ctx,
        action="support.problem_reported",
        entity_type="problem_report",
        entity_id=report.id,
        plan_id=draft.plan_id,
        metadata={"category": draft.category, "diagnostics": diagnostics is not None},
    )
    return report


async def forget_reports(ctx: CommandContext) -> None:
    """Account deletion: the person's reports go with them."""

    await ctx.session.execute(FORGET_REPORTS)


def new_code() -> str:
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"BLN-{chars[:4]}-{chars[4:]}"


async def _plan(ctx: CommandContext, plan_id: UUID) -> PlanAccess:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW)
    return access


async def _record(ctx: CommandContext, plan_id: UUID, linked: Linked) -> Any | None:
    model = RECORD_MODELS[linked.entity_type]
    record = await ctx.session.get(model, linked.entity_id)
    return record if record is not None and record.plan_id == plan_id else None


async def _ledger_problems(ctx: CommandContext, plan_id: UUID) -> int | None:
    found = await ctx.session.scalar(LEDGER_PROBLEMS, {"plan_id": plan_id})
    return int(found) if found is not None else None


async def _seq(ctx: CommandContext, scope: ChangeScope, scope_id: UUID) -> int | None:
    return await ctx.session.scalar(
        select(ScopeHead.last_seq).where(
            ScopeHead.scope_type == scope.value, ScopeHead.scope_id == scope_id
        )
    )


async def _diagnostics(
    ctx: CommandContext, draft: ReportDraft, access: PlanAccess | None, record: Any | None
) -> dict[str, Any]:
    """Identifiers, states, versions, and sequences; nothing anyone wrote."""

    actor = ctx.require_actor()
    session = await ctx.session.get(AuthSession, actor.session_id)
    snapshot: dict[str, Any] = {
        "generated_at": ctx.now.isoformat(),
        "server_release": ctx.settings.release,
        "user": {"id": str(actor.user_id), "guest": actor.is_guest},
        "session": {
            "id": str(actor.session_id),
            "auth_method": session.auth_method if session else None,
            "platform": session.platform if session else None,
            "app_version": session.app_version if session else None,
        },
        "user_scope_seq": await _seq(ctx, ChangeScope.USER, actor.user_id),
        "client": draft.client,
    }
    if access is not None:
        plan, participant = access.plan, access.participant
        head = await ctx.session.get(LedgerHead, plan.id)
        snapshot["plan"] = {
            "id": str(plan.id),
            "type": plan.type,
            "state": plan.state,
            "base_currency": plan.base_currency,
            "role": participant.role if participant else None,
            "access_state": participant.access_state if participant else None,
            "scope_seq": await _seq(ctx, ChangeScope.PLAN, plan.id),
            "ledger_seq": head.ledger_seq if head else None,
            "ledger_status": head.status if head else None,
            "ledger_problems": await _ledger_problems(ctx, plan.id),
        }
    if record is not None and draft.linked is not None:
        snapshot["record"] = {
            "type": draft.linked.entity_type,
            "id": str(draft.linked.entity_id),
            "version": getattr(record, "version", None),
            "state": getattr(record, "state", None) or getattr(record, "status", None),
            # Expenses are voided and settlements reversed rather than deleted.
            "deleted": any(
                getattr(record, column, None) is not None
                for column in ("deleted_at", "voided_at", "reversed_at")
            ),
        }
    return snapshot


async def list_reports(
    runtime: Runtime, *, operator: str, limit: int = 50, code: str | None = None
) -> list[ProblemReport]:
    """Newest reports first, or one by its code, for operators (worker role).

    Reports hold people's own words: every report read is audited with the operator.
    """

    async with open_context(runtime) as ctx:
        statement = select(ProblemReport).order_by(ProblemReport.created_at.desc()).limit(limit)
        if code is not None:
            statement = statement.where(ProblemReport.diagnostic_code == code.strip().upper())
        reports = list((await ctx.session.execute(statement)).scalars())
        for report in reports:
            await record_audit(
                ctx,
                action="support.problem_report_read",
                entity_type="problem_report",
                entity_id=report.id,
                plan_id=report.plan_id,
                metadata={"operator": operator},
            )
        return reports
