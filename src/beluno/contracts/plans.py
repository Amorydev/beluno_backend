"""Plan, participant, and RSVP contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from beluno.contracts.common import (
    CurrencyCode,
    DisplayName,
    Label,
    LongText,
    TimezoneName,
    Title,
    clean_text,
)

PlanType = Literal["trip", "hangout"]
HangoutActivity = Literal[
    "dinner", "drinks", "karaoke", "coffee", "movie", "sport", "birthday", "other"
]
PassColor = Literal["indigo", "plum", "sea", "forest", "rust", "slate", "wine", "moss"]
AvatarColor = Literal["blue", "teal", "purple", "orange", "rose", "olive"]
Capability = Literal["expenses.manage", "budgets.manage"]
DestinationName = Annotated[
    str, AfterValidator(clean_text), StringConstraints(min_length=1, max_length=80)
]
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


class Destination(BaseModel):
    """One stop of a trip ("Tokyo"); dates are optional (nights per city)."""

    model_config = ConfigDict(extra="forbid")

    name: DestinationName
    code: str | None = Field(default=None, pattern=r"^[A-Z]{1,5}$", description="e.g. TYO")
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


Destinations = Annotated[list[Destination], Field(max_length=10)]


class PlanCreateRequest(BaseModel):
    """A trip or a hangout. ``base_currency`` defaults to the creator's default currency."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    type: PlanType
    title: Title
    activity: HangoutActivity | None = Field(default=None, description="Hangouts only")
    state: Literal["draft", "planning"] = "planning"
    timing: PlanTiming = Field(default_factory=PlanTiming)
    base_currency: CurrencyCode | None = None
    destinations: Destinations = Field(default_factory=list, description="Trips only")
    pass_color: PassColor | None = None
    expected_size: int | None = Field(default=None, ge=1, le=50)
    description: LongText | None = None
    location_label: Label | None = None
    participants: list[ParticipantSeed] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def fields_fit_the_type(self) -> Self:
        if self.type == "trip" and self.activity is not None:
            raise ValueError("activity is for hangouts")
        if self.type == "hangout" and self.destinations:
            raise ValueError("destinations are for trips")
        return self


class PlanUpdateRequest(BaseModel):
    """Omitted fields stay unchanged; ``null`` clears description or location."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    activity: HangoutActivity | None = None
    timing: PlanTiming | None = None
    base_currency: CurrencyCode | None = None
    destinations: Destinations | None = None
    pass_color: PassColor | None = None
    expected_size: int | None = Field(default=None, ge=1, le=50)
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
    default_share: int = Field(description="Default shares weight in hundredths (100 = 1x)")
    avatar_color: AvatarColor
    capabilities: list[Capability]
    merged_into_participant_id: UUID | None
    joined_at: datetime | None
    version: int


class PlanResponse(BaseModel):
    id: UUID
    type: PlanType
    title: str
    activity: HangoutActivity | None
    state: PlanStateName
    timing: PlanTiming
    base_currency: str
    destinations: list[Destination]
    pass_color: PassColor
    expected_size: int | None
    description: str | None
    location_label: str | None
    deletion_scheduled_at: datetime | None
    my_participant: ParticipantResponse | None
    version: int
    created_at: datetime
    updated_at: datetime


class ParticipantAddRequest(ParticipantSeed):
    pass


class ParticipantUpdateRequest(BaseModel):
    """Managers change role, default share, and capabilities; anyone their own colour."""

    model_config = ConfigDict(extra="forbid")

    role: AssignablePlanRole | None = None
    default_share: int | None = Field(default=None, ge=1, le=10_000)
    capabilities: list[Capability] | None = Field(default=None, max_length=2)
    avatar_color: AvatarColor | None = None

    @model_validator(mode="after")
    def something_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("fields cannot be null")
        if self.capabilities is not None and len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must not repeat")
        return self


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
