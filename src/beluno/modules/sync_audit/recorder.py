"""Record audit events and change-log rows for every accepted mutation.

Records are buffered on the command context and written just before the
transaction commits (``flush_pending_records``, called by ``open_context``), so a
command that fails writes nothing. Change rows go through
``sync_audit.append_changes``, which locks each touched scope head in one sorted
order and assigns the scope's next sequence; holding those locks only at the very
end keeps them short and deadlock-free. Metadata is redacted again here so no
caller can leak tokens or contact data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from beluno.db.ids import new_id
from beluno.observability.metrics import instruments
from beluno.observability.redaction import redact

if TYPE_CHECKING:
    from beluno.modules.context import CommandContext


class ChangeScope(StrEnum):
    USER = "user"
    GROUP = "group"
    PLAN = "plan"


INSERT_AUDIT_EVENT = text(
    """
    INSERT INTO sync_audit.audit_events (
        id, occurred_at, actor_user_id, actor_session_id, request_id, action,
        entity_type, entity_id, group_id, plan_id, metadata
    ) VALUES (
        :id, :occurred_at, :actor_user_id, :actor_session_id, :request_id, :action,
        :entity_type, :entity_id, :group_id, :plan_id, CAST(:metadata AS jsonb)
    )
    """
)

APPEND_CHANGES = text("SELECT sync_audit.append_changes(CAST(:changes AS jsonb))")


def _require_tracked_savepoint(ctx: CommandContext) -> None:
    if ctx.session.in_nested_transaction() and ctx.savepoint_depth == 0:
        raise RuntimeError("record mutations inside ctx.savepoint(), not session.begin_nested()")


def _acting_user(ctx: CommandContext, actor_user_id: UUID | None) -> UUID | None:
    if actor_user_id is not None:
        return actor_user_id
    return ctx.actor.user_id if ctx.actor else ctx.on_behalf_of


async def record_mutation(
    ctx: CommandContext,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    entity_version: int,
    scope: ChangeScope,
    scope_id: UUID,
    group_id: UUID | None = None,
    plan_id: UUID | None = None,
    metadata: Mapping[str, Any] | None = None,
    operation: str = "upsert",
    actor_user_id: UUID | None = None,
) -> None:
    """Record one audit event and one change-log row for an accepted mutation."""

    _require_tracked_savepoint(ctx)
    acting_user_id = _acting_user(ctx, actor_user_id)
    ctx.pending_audit.append(
        {
            "id": new_id(),
            "occurred_at": ctx.now,
            "actor_user_id": acting_user_id,
            "actor_session_id": ctx.actor.session_id if ctx.actor else None,
            "request_id": ctx.request_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "group_id": group_id,
            "plan_id": plan_id,
            "metadata": json.dumps(redact(dict(metadata or {})), default=str, sort_keys=True),
        }
    )
    _buffer_change(
        ctx,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=entity_version,
        scope=scope,
        scope_id=scope_id,
        operation=operation,
        acting_user_id=acting_user_id,
    )


async def record_audit(
    ctx: CommandContext,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    metadata: Mapping[str, Any] | None = None,
    plan_id: UUID | None = None,
) -> None:
    """Record an audit event with no sync change (operator, maintenance, and actions
    whose sync change another record already carries)."""

    _require_tracked_savepoint(ctx)
    ctx.pending_audit.append(
        {
            "id": new_id(),
            "occurred_at": ctx.now,
            "actor_user_id": _acting_user(ctx, None),
            "actor_session_id": ctx.actor.session_id if ctx.actor else None,
            "request_id": ctx.request_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "group_id": None,
            "plan_id": plan_id,
            "metadata": json.dumps(redact(dict(metadata or {})), default=str, sort_keys=True),
        }
    )


async def record_change(
    ctx: CommandContext,
    *,
    entity_type: str,
    entity_id: UUID,
    entity_version: int,
    scope: ChangeScope,
    scope_id: UUID,
    operation: str = "upsert",
) -> None:
    """Record a change-log row without an audit event.

    For sync signals derived from a mutation that is already audited, such as a
    participant's own access entry in their user scope.
    """

    _require_tracked_savepoint(ctx)
    _buffer_change(
        ctx,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_version=entity_version,
        scope=scope,
        scope_id=scope_id,
        operation=operation,
        acting_user_id=_acting_user(ctx, None),
    )


def _buffer_change(
    ctx: CommandContext,
    *,
    entity_type: str,
    entity_id: UUID,
    entity_version: int,
    scope: ChangeScope,
    scope_id: UUID,
    operation: str,
    acting_user_id: UUID | None,
) -> None:
    ctx.pending_changes.append(
        {
            "changed_at": ctx.now.isoformat(),
            "scope_type": scope.value,
            "scope_id": str(scope_id),
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "entity_version": entity_version,
            "operation": operation,
            "actor_user_id": str(acting_user_id) if acting_user_id else None,
            "request_id": ctx.request_id,
            "operation_id": str(ctx.operation_id) if ctx.operation_id else None,
        }
    )


async def flush_pending_records(ctx: CommandContext) -> None:
    """Write buffered audit and change rows; called once, just before commit."""

    if ctx.pending_audit:
        audit_rows, ctx.pending_audit = ctx.pending_audit, []
        await ctx.session.execute(INSERT_AUDIT_EVENT, audit_rows)
    if ctx.pending_changes:
        change_rows, ctx.pending_changes = ctx.pending_changes, []
        await ctx.session.execute(APPEND_CHANGES, {"changes": json.dumps(change_rows)})
        instruments().changes_appended.add(len(change_rows))
