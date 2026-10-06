"""Sync protocol contracts: handshake, pull pages, and push batches."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SCOPE_PATTERN = r"^(user|group|plan):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
ScopeName = Annotated[str, StringConstraints(pattern=SCOPE_PATTERN, max_length=48)]
CursorToken = Annotated[str, StringConstraints(min_length=1, max_length=512)]
CommandName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]
ScopeStatus = Literal["ok", "unavailable", "resync_required"]
AccessLevelName = Literal["self", "manager", "member", "reader", "invited"]

MAX_PULL_SCOPES = 20
MAX_PULL_PAGE_SIZE = 500


class ClientInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["ios", "android", "web", "other"] | None = None
    app_version: str | None = Field(default=None, min_length=1, max_length=32)
    schema_version: int | None = Field(default=None, ge=1)


class HandshakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(ge=1)
    client: ClientInfo | None = None
    # Continues a paginated scope directory; omit to start from the user scope.
    directory_cursor: CursorToken | None = None


class ProtocolInfo(BaseModel):
    version: int
    min_version: int
    max_version: int


class SyncFeatures(BaseModel):
    push_enabled: bool
    pull_enabled: bool
    disabled_commands: list[str]


class SyncLimits(BaseModel):
    push_max_operations: int
    push_max_bytes: int
    pull_max_scopes: int
    pull_page_size: int
    pull_max_page_size: int


class RetentionInfo(BaseModel):
    offline_window_days: int
    change_retention_days: int
    operation_retention_days: int


class CommandInfo(BaseModel):
    name: str
    schema_version: int
    versioned: bool
    target_fields: list[str]


class ScopeEntry(BaseModel):
    """One scope the caller may sync right now."""

    scope: str
    head: int
    floor: int
    generation: int
    access: AccessLevelName


class HandshakeResponse(BaseModel):
    protocol: ProtocolInfo
    features: SyncFeatures
    limits: SyncLimits
    retention: RetentionInfo
    commands: list[CommandInfo]
    scopes: list[ScopeEntry]
    next_directory_cursor: str | None


class PullScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ScopeName
    # ``null`` bootstraps the scope: a paginated snapshot, then the change feed.
    cursor: CursorToken | None = None


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=1, ge=1)
    scopes: list[PullScopeRequest] = Field(min_length=1, max_length=MAX_PULL_SCOPES)
    page_size: int | None = Field(default=None, ge=10, le=MAX_PULL_PAGE_SIZE)


class ChangeItem(BaseModel):
    """One entity state to apply. ``seq`` is null for snapshot rows.

    ``data`` is the entity's public representation (the same contract the REST
    resource returns) and is null for ``delete``. Apply an upsert only when
    ``version`` is at least the locally stored version; a delete always wins.
    """

    seq: int | None
    entity_type: str
    entity_id: UUID
    operation: Literal["upsert", "delete"]
    version: int | None
    changed_at: datetime | None
    data: dict[str, Any] | None


class PullScopeResponse(BaseModel):
    scope: str
    status: ScopeStatus
    # Present for ``ok``; store it together with the applied changes in one local transaction.
    cursor: str | None
    has_more: bool
    head: int | None
    changes: list[ChangeItem]


class PullResponse(BaseModel):
    scopes: list[PullScopeResponse]
