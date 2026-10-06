"""Append audit and change records in the same transaction as each accepted mutation.

This is the seam the sync kernel extends: it will add per-scope cursors,
idempotent replay, tombstones, and outbox dispatch without changing callers.
Metadata is redacted again here so no caller can leak tokens or contact data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import text

from beluno.db.ids import new_id
from beluno.modules.context import CommandContext
from beluno.observability.redaction import redact


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

INSERT_CHANGE = text(
    """
    INSERT INTO sync_audit.change_log (
        changed_at, scope_type, scope_id, entity_type, entity_id, entity_version,
        operation, actor_user_id, request_id
    ) VALUES (
        :changed_at, :scope_type, :scope_id, :entity_type, :entity_id, :entity_version,
        :operation, :actor_user_id, :request_id
    )
    """
)


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
    """Write one audit event and one change-log entry for an accepted mutation."""

    actor = ctx.actor
    acting_user_id = actor_user_id or (actor.user_id if actor else ctx.on_behalf_of)
    await ctx.session.execute(
        INSERT_AUDIT_EVENT,
        {
            "id": new_id(),
            "occurred_at": ctx.now,
            "actor_user_id": acting_user_id,
            "actor_session_id": actor.session_id if actor else None,
            "request_id": ctx.request_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "group_id": group_id,
            "plan_id": plan_id,
            "metadata": json.dumps(redact(dict(metadata or {})), default=str, sort_keys=True),
        },
    )
    await ctx.session.execute(
        INSERT_CHANGE,
        {
            "changed_at": ctx.now,
            "scope_type": scope.value,
            "scope_id": scope_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_version": entity_version,
            "operation": operation,
            "actor_user_id": acting_user_id,
            "request_id": ctx.request_id,
        },
    )
