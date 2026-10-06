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
    # False while the finance kill switch is on: finance commands answer ``retry``.
    finance_writes_enabled: bool = True


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
    ``version`` is at least the locally stored version. A delete carries the
    version at which the row disappeared and beats every upsert with a version
    not greater than it; an upsert with a greater version revives the entity
    (a membership that was left and joined again).
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


MAX_PUSH_OPERATIONS = 1_000
PushOutcome = Literal[
    "applied", "replayed", "conflict", "rejected", "upgrade_required", "retry", "skipped"
]


class PushOperation(BaseModel):
    """One queued client mutation. ``operation_id`` doubles as its idempotency key."""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    command: CommandName
    schema_version: int = Field(default=1, ge=1)
    target: dict[str, UUID] = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=1)
    # Operation IDs (from this batch or earlier ones) that must have been applied first.
    depends_on: list[UUID] = Field(default_factory=list, max_length=20)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Untrusted metadata: the server's own sequence orders changes.
    client_created_at: datetime | None = None


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=1, ge=1)
    device_id: str | None = Field(default=None, min_length=1, max_length=64)
    operations: list[PushOperation] = Field(min_length=1, max_length=MAX_PUSH_OPERATIONS)


class PushProblem(BaseModel):
    """Why an operation did not apply; mirrors the REST problem fields."""

    status: int
    code: str
    detail: str | None = None
    details: dict[str, str] | None = None
    # ``VERSION_CONFLICT``: the canonical current representation to reconcile against.
    current: dict[str, Any] | None = None
    retry_after_seconds: int | None = None


class PushResult(BaseModel):
    """``applied``/``replayed`` carry the command's response; ``conflict``,
    ``rejected`` and ``upgrade_required`` are permanent for this payload; ``retry``
    is transient; ``skipped`` means an ordering or dependency requirement failed and
    the operation was not attempted."""

    operation_id: UUID
    outcome: PushOutcome
    status: int | None
    version: int | None
    body: dict[str, Any] | None
    problem: PushProblem | None


class PushResponse(BaseModel):
    results: list[PushResult]


class PlanAccessSignal(BaseModel):
    """User-scope signal: the caller's own participation in a plan changed.

    A state other than ``active`` means the plan scope is no longer theirs to sync
    (unless the handshake directory still lists it, e.g. as a group reader).
    """

    plan_id: UUID
    participant_id: UUID
    role: Literal["owner", "admin", "member", "viewer", "guest"]
    access_state: Literal["pending_approval", "active", "left", "removed", "merged"]
    rsvp_status: Literal["invited", "going", "maybe", "declined"]
    version: int


class GroupAccessSignal(BaseModel):
    """User-scope signal: the caller's own membership in a group changed."""

    group_id: UUID
    role: Literal["owner", "admin", "member"]
    state: Literal["invited", "active", "left", "removed"]
    version: int


class PlanEntity(BaseModel):
    """The ``plan`` entity in the feed: ``PlanResponse`` without the caller-specific
    ``my_participant`` snapshot, which the ``plan_participant`` entity carries instead."""

    id: UUID
    group_id: UUID | None
    series_id: UUID | None
    occurrence_key: str | None
    is_series_exception: bool
    title: str
    kind: str
    state: str
    timing: dict[str, Any]
    base_currency: str
    visibility: str
    description: str | None
    location_label: str | None
    deletion_scheduled_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class TravelDetailsEntity(BaseModel):
    """The ``travel_details`` entity: ``TravelResponse`` without the segments, which
    are ``travel_segment`` entities of their own."""

    plan_id: UUID
    destination_summary: str | None
    notes: str | None
    version: int
