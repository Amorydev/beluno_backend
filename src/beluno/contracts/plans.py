"""Plan, participant, RSVP, and travel-extension contracts."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

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
VisibilityName = Literal["group", "participants"]
AssignablePlanRole = Literal["admin", "member", "viewer"]
RsvpAnswer = Literal["going", "maybe", "declined"]


def wall_clock_time(value: time | None) -> time | None:
    """Series times are local wall-clock times; the zone comes from ``timezone``."""

    if value is not None and value.tzinfo is not None:
        raise ValueError("local_start_time must not include an offset; send the IANA timezone")
    return value


LocalTime = Annotated[time | None, AfterValidator(wall_clock_time)]


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
    group_id: UUID | None = None
    title: Title
    kind: PlanKind = "custom"
    state: Literal["draft", "planning"] = "planning"
    timing: PlanTiming = Field(default_factory=PlanTiming)
    base_currency: CurrencyCode | None = None
    visibility: VisibilityName | None = None
    description: LongText | None = None
    location_label: Label | None = None
    participants: list[ParticipantSeed] = Field(default_factory=list, max_length=100)
    include_all_group_members: bool = False


class PlanUpdateRequest(BaseModel):
    """Omitted fields stay unchanged; ``null`` clears description or location."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    kind: PlanKind | None = None
    timing: PlanTiming | None = None
    base_currency: CurrencyCode | None = None
    visibility: VisibilityName | None = None
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
    group_id: UUID | None
    series_id: UUID | None
    occurrence_key: str | None
    is_series_exception: bool
    title: str
    kind: PlanKind
    state: PlanStateName
    timing: PlanTiming
    base_currency: str
    visibility: VisibilityName
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


class TravelDetailsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_summary: Label | None = None
    notes: LongText | None = None


class TravelSegmentRequest(BaseModel):
    """Local wall-clock times with IANA zones (flights cross zones), or dates only.

    ``id`` is an optional client-generated ID for a new segment; updates ignore it.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    segment_type: Literal["flight", "train", "bus", "car", "ferry", "lodging", "other"]
    title: Label | None = None
    origin_label: Label | None = None
    destination_label: Label | None = None
    timing_mode: Literal["date", "datetime"]
    start_date: date | None = None
    end_date: date | None = None
    departure_local: datetime | None = None
    departure_timezone: TimezoneName | None = None
    arrival_local: datetime | None = None
    arrival_timezone: TimezoneName | None = None
    sort_order: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def consistent_fields(self) -> Self:
        local_fields = (self.departure_local, self.arrival_local)
        if any(value is not None and value.tzinfo is not None for value in local_fields):
            raise ValueError("local times must not include an offset; send the IANA zone")
        if self.timing_mode == "date":
            ok = self.start_date is not None and all(
                value is None
                for value in (
                    self.departure_local,
                    self.departure_timezone,
                    self.arrival_local,
                    self.arrival_timezone,
                )
            )
        else:
            ok = (
                self.departure_local is not None
                and self.departure_timezone is not None
                and self.start_date is None
                and self.end_date is None
                and (self.arrival_local is None) == (self.arrival_timezone is None)
            )
        if not ok:
            raise ValueError(f"segment fields are inconsistent with '{self.timing_mode}'")
        return self


class TravelSegmentResponse(BaseModel):
    id: UUID
    segment_type: str
    title: str | None
    origin_label: str | None
    destination_label: str | None
    timing_mode: Literal["date", "datetime"]
    start_date: date | None
    end_date: date | None
    departure_local: datetime | None
    departure_timezone: str | None
    departs_at: datetime | None
    arrival_local: datetime | None
    arrival_timezone: str | None
    arrives_at: datetime | None
    sort_order: int
    version: int


class TravelResponse(BaseModel):
    plan_id: UUID
    destination_summary: str | None
    notes: str | None
    version: int
    segments: list[TravelSegmentResponse]


class PlanDuplicateRequest(BaseModel):
    """Copies only the allowlisted structure; see the ``plan-copy-v1`` manifest.

    Omit ``group_id`` to keep the source group; send ``null`` for no group.
    ``participant_ids`` omitted copies every active registered participant;
    an empty list copies none.
    """

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    group_id: UUID | None = None
    timing: PlanTiming = Field(default_factory=PlanTiming)
    participant_ids: list[UUID] | None = Field(default=None, max_length=100)
    include_description: bool = True
    include_location: bool = False
    include_travel_details: bool = True


class SeriesCreateRequest(BaseModel):
    """A recurring plan. ``recurrence_rule`` is an RRULE subset, e.g.
    ``FREQ=WEEKLY;BYDAY=TH`` or ``FREQ=MONTHLY;BYDAY=-1FR;COUNT=6``."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    group_id: UUID | None = None
    title: Title
    kind: PlanKind = "custom"
    base_currency: CurrencyCode | None = None
    visibility: VisibilityName | None = None
    description: LongText | None = None
    location_label: Label | None = None
    timezone: TimezoneName
    start_date: date
    local_start_time: LocalTime = None
    duration_minutes: int | None = Field(default=None, ge=1, le=10_080)
    recurrence_rule: str = Field(min_length=6, max_length=500)
    participant_user_ids: list[UUID] = Field(default_factory=list, max_length=100)
    horizon_days: int = Field(default=56, ge=7, le=366)


class SeriesSplitRequest(BaseModel):
    """Change this and future occurrences from ``from_date``; earlier ones stay as they are."""

    model_config = ConfigDict(extra="forbid")

    from_date: date
    title: Title | None = None
    local_start_time: LocalTime = None
    duration_minutes: int | None = Field(default=None, ge=1, le=10_080)
    recurrence_rule: str | None = Field(default=None, min_length=6, max_length=500)
    timezone: TimezoneName | None = None


class SeriesResponse(BaseModel):
    id: UUID
    group_id: UUID | None
    title: str
    kind: PlanKind
    base_currency: str
    visibility: VisibilityName
    timezone: str
    start_date: date
    local_start_time: time | None
    duration_minutes: int | None
    recurrence_rule: str
    horizon_days: int
    materialized_through: date | None
    state: Literal["active", "cancelled"]
    version: int
    created_at: datetime


class SeriesWithOccurrencesResponse(BaseModel):
    series: SeriesResponse
    occurrences: list[PlanResponse]
