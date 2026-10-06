"""Push: apply a batch of queued client operations, one transaction each.

Every operation gets its own result. Client order is preserved: after a
transient failure (rate limit, disabled feature, exhausted retries) the later
operations that address the same plan, group, or series are skipped so they
cannot overtake the failed one; a permanent failure only skips operations that
declared a dependency on it. Replays of already-applied operations come back
with their stored response and never count as new work.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from beluno.auth import AuthenticatedActor
from beluno.contracts.errors import BelunoError, validation_error
from beluno.contracts.sync import (
    PushOperation,
    PushOutcome,
    PushProblem,
    PushRequest,
    PushResult,
)
from beluno.db.models.sync_audit import OperationRecord
from beluno.modules.context import open_context
from beluno.sync.commands import Command, CommandCall
from beluno.sync.executor import CommandRunner
from beluno.sync.idempotency import key_reused

ORDERING_FIELDS = ("plan_id", "group_id", "series_id")
TRANSIENT_STATUSES = frozenset({429, 503})
PERMANENT_CONFLICT_STATUSES = frozenset({409, 412})


def ordering_key(operation: PushOperation) -> str:
    """Operations with the same key must apply in the order the client sent them."""

    for field in ORDERING_FIELDS:
        if field in operation.target:
            return f"{field}:{operation.target[field]}"
    return "user"


async def push_batch(
    runner: CommandRunner, actor: AuthenticatedActor, request: PushRequest
) -> list[PushResult]:
    operations = request.operations
    ids = [operation.operation_id for operation in operations]
    if len(set(ids)) != len(ids):
        raise validation_error("operation_id values must be unique within a batch")
    external = {dep for op in operations for dep in op.depends_on} - set(ids)
    stored = await _stored_commands(runner, actor, external | set(ids))
    applied = {operation_id for operation_id in external if operation_id in stored}
    blocked: set[str] = set()
    results: list[PushResult] = []
    for operation in operations:
        earlier = stored.get(operation.operation_id)
        if earlier is not None and earlier != operation.command:
            results.append(_failed(operation, key_reused()))
            continue
        key = ordering_key(operation)
        if key in blocked:
            results.append(
                _skipped(operation, "an earlier operation on the same scope must be retried first")
            )
            continue
        missing = [dep for dep in operation.depends_on if dep not in applied]
        if missing:
            results.append(
                _skipped(operation, f"depends on operation {missing[0]}, which was not applied")
            )
            continue
        result = await _execute(runner, actor, operation, request.device_id)
        results.append(result)
        if result.outcome in ("applied", "replayed"):
            applied.add(operation.operation_id)
        elif result.outcome == "retry":
            blocked.add(key)
    return results


async def _stored_commands(
    runner: CommandRunner, actor: AuthenticatedActor, operation_ids: Iterable[UUID]
) -> dict[UUID, str]:
    """The command each known operation ID was applied with (earlier batches included)."""

    keys = [str(operation_id) for operation_id in operation_ids]
    if not keys:
        return {}
    async with open_context(runner.runtime, actor) as ctx:
        rows = await ctx.session.execute(
            select(OperationRecord.idempotency_key, OperationRecord.command).where(
                OperationRecord.actor_user_id == actor.user_id,
                OperationRecord.idempotency_key.in_(keys),
            )
        )
    return {UUID(key): command for key, command in rows.all()}


async def _execute(
    runner: CommandRunner,
    actor: AuthenticatedActor,
    operation: PushOperation,
    device_id: str | None,
) -> PushResult:
    try:
        command = runner.registry.resolve(operation.command, operation.schema_version)
        payload = _validate(command, operation)
        call = CommandCall(
            target=operation.target,
            expected_version=operation.expected_version,
            idempotency_key=str(operation.operation_id),
            source="push",
            device_id=device_id,
            client_created_at=operation.client_created_at,
        )
        result = await runner.run(actor, command, call, payload)
    except BelunoError as error:
        if error.status == 401:
            raise
        return _failed(operation, error)
    body = result.body.model_dump(mode="json") if isinstance(result.body, BaseModel) else None
    return PushResult(
        operation_id=operation.operation_id,
        outcome="replayed" if result.replayed else "applied",
        status=result.status,
        version=result.etag_version,
        body=body,
        problem=None,
    )


def _validate(command: Command[Any, Any], operation: PushOperation) -> BaseModel:
    expected = set(command.target_fields)
    if set(operation.target) != expected:
        wanted = ", ".join(sorted(expected)) or "none"
        raise validation_error(f"{command.name} target must contain exactly: {wanted}")
    try:
        payload: BaseModel = command.payload_model.model_validate(operation.payload)
    except ValidationError as error:
        details = {
            ".".join(str(part) for part in item["loc"]): str(item["msg"])
            for item in error.errors()[:20]
        }
        problem = validation_error("payload does not satisfy the command contract")
        problem.details = details or None
        raise problem from error
    return payload


def _failed(operation: PushOperation, error: BelunoError) -> PushResult:
    retry_after = None
    if error.headers and "Retry-After" in error.headers:
        retry_after = int(error.headers["Retry-After"])
    problem = PushProblem(
        status=error.status,
        code=error.code,
        detail=error.detail,
        details=error.details,
        current=error.current,
        retry_after_seconds=retry_after,
    )
    outcome: PushOutcome
    if error.status == 426:
        outcome = "upgrade_required"
    elif error.status in TRANSIENT_STATUSES:
        outcome = "retry"
    elif error.status in PERMANENT_CONFLICT_STATUSES:
        outcome = "conflict"
    else:
        outcome = "rejected"
    return PushResult(
        operation_id=operation.operation_id,
        outcome=outcome,
        status=error.status,
        version=None,
        body=None,
        problem=problem,
    )


def _skipped(operation: PushOperation, detail: str) -> PushResult:
    return PushResult(
        operation_id=operation.operation_id,
        outcome="skipped",
        status=None,
        version=None,
        body=None,
        problem=PushProblem(status=409, code="OPERATION_SKIPPED", detail=detail),
    )
