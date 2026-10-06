"""Run one command: retries, idempotent replay, and in-transaction rendering.

    BEGIN
      re-check session; set actor context           (open_context)
      lock the idempotency scope; replay if stored   (idempotency.reserve)
      run the handler, which authorizes and mutates
      render the response                            (inside the handler)
      store the canonical outcome                    (idempotency.store)
      write buffered audit/change rows               (open_context exit)
    COMMIT

Serialization failures and deadlocks (SQLSTATE 40001/40P01) are retried a
bounded number of times with a fresh transaction; validation, authorization,
and domain errors never are.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError

from beluno.auth import AuthenticatedActor
from beluno.contracts.errors import (
    BelunoError,
    VersionConflictError,
    feature_disabled,
    precondition_required,
)
from beluno.modules.context import Runtime, open_context
from beluno.modules.iam.rate_limits import enforce_rate_limit
from beluno.observability.metrics import attributes, instruments
from beluno.sync import idempotency
from beluno.sync.commands import (
    Command,
    CommandCall,
    CommandRegistry,
    CommandResult,
    PayloadT,
    ResponseT,
)

RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.02


def is_retryable(error: BaseException) -> bool:
    origin = error.orig if isinstance(error, DBAPIError) else error
    return getattr(origin, "sqlstate", None) in RETRYABLE_SQLSTATES


def retry_later() -> BelunoError:
    """The transaction kept conflicting; the write did not commit and may be resent."""

    return BelunoError(
        status=503,
        code="RETRY_LATER",
        title="The request hit a transient database conflict",
        detail="Nothing was changed; retry the same request unchanged",
        headers={"Retry-After": "1"},
    )


class CommandRunner:
    def __init__(self, runtime: Runtime, registry: CommandRegistry) -> None:
        self.runtime = runtime
        self.registry = registry

    async def run(
        self,
        actor: AuthenticatedActor,
        command: Command[PayloadT, ResponseT],
        call: CommandCall,
        payload: PayloadT,
    ) -> CommandResult[ResponseT]:
        settings = self.runtime.settings
        if command.name in settings.sync_disabled_commands:
            raise feature_disabled()
        if command.feature == "finance" and not settings.finance_writes_enabled:
            raise feature_disabled()
        if command.versioned and call.expected_version is None:
            raise precondition_required()
        if command.rate_limit is not None and not await self._is_replay(actor, command, call):
            subject = str(actor.user_id)
            if command.rate_limit_target is not None:
                subject = f"{subject}:{call.id(command.rate_limit_target)}"
            await enforce_rate_limit(self.runtime, command.rate_limit, subject)
        started = time.perf_counter()
        attempt = 1
        meters = instruments()
        try:
            while True:
                try:
                    result = await self._attempt(actor, command, call, payload)
                    break
                except Exception as error:
                    if not is_retryable(error):
                        raise
                    if attempt >= MAX_ATTEMPTS:
                        raise retry_later() from error
                meters.command_retries.add(1, attributes(command=command.name))
                await asyncio.sleep(random.uniform(0, RETRY_BACKOFF_SECONDS * attempt))
                attempt += 1
        except BelunoError as error:
            meters.commands.add(
                1,
                attributes(command=command.name, source=call.source, outcome=error.code.lower()),
            )
            raise
        finally:
            meters.command_duration.record(
                (time.perf_counter() - started) * 1_000, attributes(command=command.name)
            )
        meters.commands.add(
            1,
            attributes(
                command=command.name,
                source=call.source,
                outcome="replayed" if result.replayed else "applied",
            ),
        )
        return result

    async def _is_replay(
        self, actor: AuthenticatedActor, command: Command[Any, Any], call: CommandCall
    ) -> bool:
        """Peek (in its own short transaction) so a replay does not count against limits."""

        if call.idempotency_key is None:
            return False
        async with open_context(self.runtime, actor) as ctx:
            record = await idempotency.find(ctx, actor.user_id, command.name, call.idempotency_key)
        return record is not None

    async def _attempt(
        self,
        actor: AuthenticatedActor,
        command: Command[PayloadT, ResponseT],
        call: CommandCall,
        payload: PayloadT,
    ) -> CommandResult[ResponseT]:
        async with open_context(self.runtime, actor) as ctx:
            operation_id = None
            if call.idempotency_key is not None:
                stored = await idempotency.reserve(ctx, command, call, payload)
                if stored is not None:
                    return CommandResult(
                        status=stored.status,
                        body=_revive(command, stored.body),
                        etag_version=stored.etag_version,
                        replayed=True,
                        operation_id=stored.operation_id,
                    )
                operation_id = idempotency.new_operation_id()
                ctx.operation_id = operation_id
            try:
                body = await command.handler(ctx, call, payload)
            except VersionConflictError as error:
                if error.entity is not None and error.current is None:
                    current = await self.registry.present_conflict(ctx, error.entity)
                    error.current = current.model_dump(mode="json") if current else None
                raise
            etag_version = command.etag(body) if command.etag is not None else None
            if operation_id is not None:
                await idempotency.store(
                    ctx,
                    command,
                    call,
                    payload,
                    operation_id=operation_id,
                    status=command.status,
                    body=body,
                    etag_version=etag_version,
                )
            return CommandResult(
                status=command.status,
                body=body,
                etag_version=etag_version,
                replayed=False,
                operation_id=operation_id,
            )


def _revive(command: Command[Any, ResponseT], body: dict[str, Any] | None) -> ResponseT:
    """Rebuild the stored response through its contract model."""

    if command.response_model is None or body is None:
        return None  # type: ignore[return-value]
    revived: BaseModel = command.response_model.model_validate(body)
    return revived  # type: ignore[return-value]
