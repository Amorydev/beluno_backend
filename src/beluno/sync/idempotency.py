"""Idempotent command outcomes.

Unique scope is ``(actor, command, key)``. A transaction-scoped advisory lock on
that scope serializes concurrent duplicates, so a retry that overlaps the first
attempt waits and then replays its committed response instead of running twice.
Only successful outcomes are stored; the record commits with the domain write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, text

from beluno.contracts.errors import BelunoError
from beluno.db.ids import new_id
from beluno.db.models.sync_audit import OperationRecord
from beluno.modules.context import CommandContext
from beluno.sync.commands import Command, CommandCall

ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:key)")


@dataclass(frozen=True)
class StoredOutcome:
    status: int
    body: dict[str, Any] | None
    etag_version: int | None
    operation_id: UUID


def request_hash(command: Command[Any, Any], call: CommandCall, payload: BaseModel) -> bytes:
    """Canonical digest of everything that makes two requests the same command."""

    canonical = {
        "command": command.name,
        "schema_version": command.schema_version,
        "target": {name: str(value) for name, value in sorted(call.target.items())},
        "expected_version": call.expected_version,
        "payload": payload.model_dump(mode="json", exclude_unset=True),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).digest()


def key_reused() -> BelunoError:
    return BelunoError(
        status=409,
        code="IDEMPOTENCY_KEY_REUSED",
        title="Idempotency key was already used with a different request",
    )


def _lock_key(actor_id: UUID, command: str, key: str) -> int:
    digest = hashlib.sha256(f"{actor_id}\x00{command}\x00{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def reserve(
    ctx: CommandContext,
    command: Command[Any, Any],
    call: CommandCall,
    payload: BaseModel,
) -> StoredOutcome | None:
    """Serialize on the key and return the stored outcome of an earlier identical request."""

    assert call.idempotency_key is not None
    actor = ctx.require_actor()
    await ctx.session.execute(
        ADVISORY_LOCK, {"key": _lock_key(actor.user_id, command.name, call.idempotency_key)}
    )
    existing = await find(ctx, actor.user_id, command.name, call.idempotency_key)
    if existing is None:
        return None
    if existing.request_hash != request_hash(command, call, payload):
        raise key_reused()
    return StoredOutcome(
        status=existing.response_status,
        body=existing.response_body,
        etag_version=int(existing.response_etag) if existing.response_etag else None,
        operation_id=existing.id,
    )


async def find(
    ctx: CommandContext, actor_id: UUID, command: str, key: str
) -> OperationRecord | None:
    return (
        await ctx.session.execute(
            select(OperationRecord).where(
                OperationRecord.actor_user_id == actor_id,
                OperationRecord.command == command,
                OperationRecord.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


async def store(
    ctx: CommandContext,
    command: Command[Any, Any],
    call: CommandCall,
    payload: BaseModel,
    *,
    operation_id: UUID,
    status: int,
    body: BaseModel | None,
    etag_version: int | None,
) -> None:
    assert call.idempotency_key is not None
    actor = ctx.require_actor()
    retention = timedelta(days=ctx.settings.sync_operation_retention_days)
    ctx.session.add(
        OperationRecord(
            id=operation_id,
            actor_user_id=actor.user_id,
            command=command.name,
            idempotency_key=call.idempotency_key,
            request_hash=request_hash(command, call, payload),
            source=call.source,
            session_id=actor.session_id,
            device_id=call.device_id,
            client_created_at=call.client_created_at,
            response_status=status,
            response_body=body.model_dump(mode="json") if body is not None else None,
            response_etag=str(etag_version) if etag_version is not None else None,
            created_at=ctx.now,
            expires_at=ctx.now + retention,
        )
    )
    await ctx.session.flush()


def new_operation_id() -> UUID:
    return new_id()
