"""Exports of a plan's data and of an account's data, built from the sync snapshot.

An export holds exactly what the caller could already sync: the same entity
types for their access level, rendered by the same presenters, so secrets
(booking codes and notes, invite tokens) and other people's private packing
items never appear. The data is read inside the request's transaction; the file
is rendered after it closes (``ExportFile.render``, off the event loop), and
nothing is stored.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select

from beluno.api.projection import FeedProjector
from beluno.contracts.errors import not_found
from beluno.db.models.finance import Currency
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope, record_audit
from beluno.sync.scopes import AccessLevel, ScopeKey, scope_access

EXPORT_FORMAT = "beluno.export"
EXPORT_VERSION = 1
PAGE_SIZE = 500
FUND = "Kitty"
# Also the full-width forms some spreadsheets read as the same signs.
FORMULA_STARTS = frozenset("=+-@\t\r\uff1d\uff0b\uff0d\uff20")
CSV_COLUMNS = (
    "date",
    "description",
    "category",
    "amount",
    "currency",
    "base_amount",
    "base_currency",
    "rate",
    "rate_source",
    "paid_by",
    "split",
    "refunded",
    "state",
    "expense_id",
)

Entities = dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class ExportFile:
    filename: str
    media_type: str
    render: Callable[[], bytes]  # pure: safe to run in a worker thread


async def plan_json(ctx: CommandContext, plan_id: UUID) -> ExportFile:
    """Everything the caller syncs for the plan, plus their own private packing items."""

    entities = await _plan_entities(ctx, plan_id)
    await record_audit(
        ctx,
        action="plan.exported",
        entity_type="plan",
        entity_id=plan_id,
        plan_id=plan_id,
        metadata={"format": "json"},
    )
    body = {"plan_id": str(plan_id), "entities": entities}
    return ExportFile(f"beluno-plan-{plan_id}.json", "application/json", _document(ctx, body))


async def plan_csv(ctx: CommandContext, plan_id: UUID) -> ExportFile:
    """One row per expense: the original amount and its base-currency snapshot."""

    entities = await _plan_entities(ctx, plan_id)
    await record_audit(
        ctx,
        action="plan.exported",
        entity_type="plan",
        entity_id=plan_id,
        plan_id=plan_id,
        metadata={"format": "csv"},
    )
    names = {row["id"]: row["display_name"] for row in entities.get("plan_participant", [])}
    exponents = dict((await ctx.session.execute(select(Currency.code, Currency.exponent))).all())
    # UUIDv7 IDs order expenses recorded on the same day by when they were made.
    expenses = sorted(
        entities.get("expense", []),
        key=lambda row: (row["revision"]["occurred_on"], row["id"]),
    )

    def render() -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for expense in expenses:
            writer.writerow(_expense_row(expense, names, exponents))
        # Excel opens UTF-8 CSV correctly only with a byte-order mark.
        return buffer.getvalue().encode("utf-8-sig")

    return ExportFile(f"beluno-plan-{plan_id}.csv", "text/csv; charset=utf-8", render)


async def account_json(ctx: CommandContext) -> ExportFile:
    """The account's own scope and every plan it can still sync."""

    user_id = ctx.require_actor().user_id
    mine = await _scope_entities(ctx, ScopeKey(ChangeScope.USER, user_id), AccessLevel.SELF)
    plans: dict[str, Entities] = {}
    for signal in mine.get("plan_access", []):
        scope = ScopeKey(ChangeScope.PLAN, UUID(signal["plan_id"]))
        level = await scope_access(ctx, scope)
        if level is not None:
            plans[signal["plan_id"]] = await _scope_entities(ctx, scope, level)
    await record_audit(ctx, action="account.exported", entity_type="user", entity_id=user_id)
    body = {"user_id": str(user_id), "entities": mine, "plans": plans}
    return ExportFile(f"beluno-account-{user_id}.json", "application/json", _document(ctx, body))


async def _plan_entities(ctx: CommandContext, plan_id: UUID) -> Entities:
    scope = ScopeKey(ChangeScope.PLAN, plan_id)
    level = await scope_access(ctx, scope)
    if level is None:
        raise not_found()
    entities = await _scope_entities(ctx, scope, level)
    own = ScopeKey(ChangeScope.USER, ctx.require_actor().user_id)
    private = await _all_rows(ctx, own, AccessLevel.SELF, "packing_item")
    entities["packing_item"] = [
        *entities.get("packing_item", []),
        *(row for row in private if row["plan_id"] == str(plan_id)),
    ]
    return entities


async def _scope_entities(ctx: CommandContext, scope: ScopeKey, level: AccessLevel) -> Entities:
    projector = FeedProjector()
    visible = projector.visible_types(scope.scope_type.value, level)
    return {
        entity_type: await _all_rows(ctx, scope, level, entity_type)
        for entity_type in projector.snapshot_order(scope.scope_type.value)
        if entity_type in visible
    }


async def _all_rows(
    ctx: CommandContext, scope: ScopeKey, level: AccessLevel, entity_type: str
) -> list[dict[str, Any]]:
    projector = FeedProjector()
    rows: list[dict[str, Any]] = []
    after: UUID | None = None
    while True:
        page = await projector.snapshot_page(ctx, scope, level, entity_type, after, PAGE_SIZE)
        rows.extend(row.data.model_dump(mode="json") for row in page)
        if len(page) < PAGE_SIZE:
            return rows
        after = page[-1].entity_id


def _document(ctx: CommandContext, body: dict[str, Any]) -> Callable[[], bytes]:
    header = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": _timestamp(ctx.now),
    }
    return lambda: json.dumps({**header, **body}, ensure_ascii=False).encode("utf-8")


def _timestamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _text(value: str) -> str:
    """Free text that a spreadsheet would read as a formula is kept as text."""

    return f"'{value}" if value[:1] in FORMULA_STARTS else value


def _expense_row(
    expense: dict[str, Any], names: dict[str, str], exponents: dict[str, int]
) -> list[str]:
    revision = expense["revision"]
    currency, base = revision["currency"], revision["base"]

    def amount(minor: int | None, code: str) -> str:
        if minor is None:
            return ""
        return str(Decimal(minor).scaleb(-exponents.get(code, 2)))

    def who(participant_id: str | None) -> str:
        return FUND if participant_id is None else names.get(participant_id, participant_id)

    paid_by = _text(
        "; ".join(
            f"{who(payer['participant_id'])}: {amount(payer['amount_minor'], currency)}"
            for payer in revision["payers"]
        )
    )
    split = _text(
        "; ".join(
            f"{who(share['participant_id'])}: {amount(share['owed_minor'], currency)}"
            for share in revision["shares"]
        )
    )
    return [
        revision["occurred_on"],
        _text(revision["description"]),
        revision["category"],
        amount(revision["amount_minor"], currency),
        currency,
        amount(base["amount_minor"], base["currency"]),
        base["currency"],
        base["rate"] or "",
        base["rate_source"] or "",
        paid_by,
        split,
        amount(expense["refunded_minor"], currency) if expense["refunded_minor"] else "",
        expense["state"],
        expense["id"],
    ]
