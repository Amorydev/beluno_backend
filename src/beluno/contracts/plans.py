"""Plan, participant, and RSVP contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from beluno.contracts.common import (
    CurrencyCode,
    DisplayName,
    Label,
    LongText,
    TimezoneName,
    Title,
)

PlanKind = Literal["dinner", "coffee", "movie", "sport", "birthday", "outing", "trip", "custom"]
PlanStateName = Literal[
    "draft", "planning", "active", "settling", "completed", "archived", "cancelled"
]
AssignablePlanRole = Literal["admin", "member", "viewer"]
RsvpAnswer = Literal["going", "maybe", "declined"]


class PlanTiming(BaseModel):
    """``undecided``; ``date`` (start_date, optional end_date); or ``datetime``
    (starts_at with offset, optional ends_at, IANA timezone)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["undecided", "date", "datetime"] = "undecided"
    start_date: date | None = None
    end_date: date | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    timezone: TimezoneName | None = None

    @model_validator(mode="after")
    def consistent_fields(self) -> Self:
        if self.mode == "undecided":
            ok = self.start_date is self.end_date is self.starts_at is self.ends_at is None
        elif self.mode == "date":
            ok = (
                self.start_date is not None
                and self.starts_at is None
                and self.ends_at is None
                and (self.end_date is None or self.end_date >= self.start_date)
            )
        else:
            ok = (
                self.starts_at is not None
                and self.timezone is not None
                and self.start_date is None
                and self.end_date is None
                and (self.ends_at is None or self.ends_at >= self.starts_at)
            )
        if not ok:
            raise ValueError(f"timing fields are inconsistent with mode '{self.mode}'")
        return self


class ParticipantSeed(BaseModel):
    """A registered person (``user_id``) or a name-only placeholder.

    ``id`` is an optional client-generated participant ID; it is ignored when the
    person already has a participant row in the plan (that row is reused).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    user_id: UUID | None = None
    placeholder_name: DisplayName | None = None
    role: AssignablePlanRole = "member"

    @model_validator(mode="after")
    def exactly_one_identity(self) -> Self:
        if (self.user_id is None) == (self.placeholder_name is None):
            raise ValueError("provide user_id or placeholder_name")
        return self


class PlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    title: Title
    kind: PlanKind = "custom"
    state: Literal["draft", "planning"] = "planning"
    timing: PlanTiming = Field(default_factory=PlanTiming)
    base_currency: CurrencyCode
    description: LongText | None = None
    location_label: Label | None = None
    participants: list[ParticipantSeed] = Field(default_factory=list, max_length=100)


class PlanUpdateRequest(BaseModel):
    """Omitted fields stay unchanged; ``null`` clears description or location."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    kind: PlanKind | None = None
    timing: PlanTiming | None = None
    base_currency: CurrencyCode | None = None
    description: LongText | None = None
    location_label: Label | None = None


class PlanStateChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: PlanStateName


class ParticipantResponse(BaseModel):
    id: UUID
    plan_id: UUID
    identity_kind: Literal["user", "guest", "placeholder"]
    user_id: UUID | None
    display_name: str
    role: Literal["owner", "admin", "member", "viewer", "guest"]
    access_state: Literal["pending_approval", "active", "left", "removed", "merged"]
    rsvp_status: Literal["invited", "going", "maybe", "declined"]
    rsvp_updated_at: datetime | None
    merged_into_participant_id: UUID | None
    joined_at: datetime | None
    version: int


class PlanResponse(BaseModel):
    id: UUID
    title: str
    kind: PlanKind
    state: PlanStateName
    timing: PlanTiming
    base_currency: str
    description: str | None
    location_label: str | None
    deletion_scheduled_at: datetime | None
    my_participant: ParticipantResponse | None
    version: int
    created_at: datetime
    updated_at: datetime


class ParticipantAddRequest(ParticipantSeed):
    pass


class ParticipantRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AssignablePlanRole


class RsvpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RsvpAnswer


class JoinRequestDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool


class PlanOwnershipTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_owner_participant_id: UUID


class PlanDuplicateRequest(BaseModel):
    """Copies only the allowlisted structure; see the ``plan-copy-v1`` manifest.

    ``participant_ids`` omitted copies every active registered participant;
    an empty list copies none.
    """

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    timing: PlanTiming = Field(default_factory=PlanTiming)
    participant_ids: list[UUID] | None = Field(default=None, max_length=100)
    include_description: bool = True
    include_location: bool = False
