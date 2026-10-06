"""The command catalog shared by REST routes and sync push.

A command names one domain mutation: its payload contract, the IDs that address
its target, the response it renders inside the transaction, and the handler
that performs it. REST routes and push items both resolve to the same command,
so a mutation behaves identically whichever way it arrives.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from beluno.contracts.errors import BelunoError
from beluno.modules.context import CommandContext
from beluno.modules.iam.rate_limits import RateLimit

PayloadT = TypeVar("PayloadT", bound=BaseModel)
ResponseT = TypeVar("ResponseT", bound="BaseModel | None")

# Only this version window is accepted; older clients must upgrade (426).
PROTOCOL_VERSION_MIN = 1
PROTOCOL_VERSION_MAX = 1


class EmptyPayload(BaseModel):
    """Commands that need no body still take a payload so hashing stays uniform."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class CommandCall:
    """Everything about one invocation that is not the payload."""

    target: Mapping[str, UUID] = field(default_factory=dict)
    expected_version: int | None = None
    idempotency_key: str | None = None
    source: str = "http"
    device_id: str | None = None
    client_created_at: datetime | None = None

    def id(self, name: str) -> UUID:
        return self.target[name]


Handler = Callable[[CommandContext, CommandCall, PayloadT], Awaitable[ResponseT]]
ConflictPresenter = Callable[[CommandContext, object], Awaitable[BaseModel | None]]


@dataclass(frozen=True)
class Command(Generic[PayloadT, ResponseT]):
    name: str
    payload_model: type[PayloadT]
    response_model: type[BaseModel] | None
    handler: Handler[PayloadT, ResponseT]
    target_fields: tuple[str, ...] = ()
    status: int = 200
    schema_version: int = 1
    # The command checks an expected entity version (``If-Match`` on REST).
    versioned: bool = False
    rate_limit: RateLimit | None = None
    # Reads the ETag version from a rendered response; None when there is none.
    etag: Callable[[Any], int] | None = None


@dataclass(frozen=True)
class CommandResult(Generic[ResponseT]):
    status: int
    body: ResponseT
    etag_version: int | None
    replayed: bool
    operation_id: UUID | None


def client_upgrade_required(detail: str) -> BelunoError:
    return BelunoError(
        status=426,
        code="CLIENT_UPGRADE_REQUIRED",
        title="Client protocol is not supported",
        detail=detail,
    )


def unknown_command(name: str) -> BelunoError:
    return BelunoError(
        status=422,
        code="VALIDATION_FAILED",
        title="Request validation failed",
        detail=f"Unknown command {name!r}",
    )


class CommandRegistry:
    """Immutable catalog plus the presenter that renders stale-version snapshots."""

    def __init__(
        self,
        commands: list[Command[Any, Any]],
        *,
        present_conflict: ConflictPresenter,
    ) -> None:
        self._commands: dict[str, Command[Any, Any]] = {}
        for command in commands:
            if command.name in self._commands:
                raise ValueError(f"duplicate command {command.name}")
            self._commands[command.name] = command
        self.present_conflict = present_conflict

    def resolve(self, name: str, schema_version: int) -> Command[Any, Any]:
        command = self._commands.get(name)
        if command is None:
            raise unknown_command(name)
        if schema_version != command.schema_version:
            raise client_upgrade_required(
                f"{name} schema version {schema_version} is not supported; "
                f"use {command.schema_version}"
            )
        return command

    def catalog(self) -> list[Command[Any, Any]]:
        return sorted(self._commands.values(), key=lambda command: command.name)


def version_of(body: Any) -> int:
    version = getattr(body, "version", None)
    if not isinstance(version, int):
        raise TypeError("response has no integer version for an ETag")
    return version
