"""Trip planning contracts: saved places and the itinerary.

Edits are whole-entity replacements (``PUT``) checked against ``If-Match``, so an
offline device sends exactly what it shows. Ordering keys are the fractional keys
of ``beluno.sync.ordering``; a device computes one between its neighbours.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from beluno.contracts.common import CurrencyCode, LongText, TimezoneName, Title
from beluno.contracts.finance import ExpenseCategory, Note

PlaceCategory = Literal["food", "sight", "stay", "activity", "shopping", "nightlife", "other"]
PlaceStatus = Literal["shortlist", "in_plan", "poll_winner"]
ItemStatus = Literal["planned", "done", "cancelled"]
Attendance = Literal["going", "not_going"]
MapsUrl = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=2000, strip_whitespace=True, pattern=r"^[Hh][Tt][Tt][Pp][Ss]?://"
    ),
]
# Up to 64 characters from devices, so appending after any key stays within the 128 stored.
OrderKey = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z]*[1-9A-Za-z]$")
]


class PlaceRequest(BaseModel):
    """A saved place. A Maps link is read for coordinates offline; it is never fetched."""

    model_config = ConfigDict(extra="forbid")

    name: Title
    maps_url: MapsUrl | None = None
    category: PlaceCategory = "other"
    note: Note | None = None


class PlaceCreateRequest(PlaceRequest):
    id: UUID | None = None


class PlaceReactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wants: bool


class EstimatedCost(BaseModel):
    """What the item is expected to cost; budgets count it until an expense pays it."""

    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode
    amount_minor: StrictInt = Field(gt=0)
    category: ExpenseCategory | None = Field(
        default=None, description="Omitted keeps the current category (activities for a new cost)"
    )


class ItineraryItemRequest(BaseModel):
    """An item on a day (or anytime when ``day`` is null), optionally at a local time."""

    model_config = ConfigDict(extra="forbid")

    title: Title
    day: date | None = None
    start_time: time | None = None
    timezone: TimezoneName | None = Field(
        default=None, description="Zone of start_time; defaults to the plan's timezone"
    )
    duration_minutes: int | None = Field(default=None, ge=1, le=10_080)
    note: LongText | None = None
    place_id: UUID | None = None
    lead_participant_id: UUID | None = None
    booking_id: UUID | None = None
    status: ItemStatus = "planned"
    order_key: OrderKey | None = Field(
        default=None, description="Position within the day; omitted puts the item last"
    )
    estimated_cost: EstimatedCost | None = None

    @model_validator(mode="after")
    def _time_needs_a_day(self) -> ItineraryItemRequest:
        if self.start_time is not None and self.start_time.tzinfo is not None:
            raise ValueError("start_time is a local time; put the zone in timezone")
        if self.start_time is not None and self.day is None:
            raise ValueError("start_time needs a day")
        if self.timezone is not None and self.start_time is None:
            raise ValueError("timezone belongs to start_time")
        return self


class ItineraryItemCreateRequest(ItineraryItemRequest):
    id: UUID | None = None


class AttendanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Attendance


class AddPlaceToPlanRequest(BaseModel):
    """Put a saved place on the itinerary (it becomes ``in_plan``)."""

    model_config = ConfigDict(extra="forbid")

    item_id: UUID | None = None
    day: date | None = None
    start_time: time | None = None
    timezone: TimezoneName | None = None

    @model_validator(mode="after")
    def _time_needs_a_day(self) -> AddPlaceToPlanRequest:
        if self.start_time is not None and self.start_time.tzinfo is not None:
            raise ValueError("start_time is a local time; put the zone in timezone")
        if self.start_time is not None and self.day is None:
            raise ValueError("start_time needs a day")
        if self.timezone is not None and self.start_time is None:
            raise ValueError("timezone belongs to start_time")
        return self


class PlaceResponse(BaseModel):
    id: UUID
    plan_id: UUID
    name: str
    maps_url: str | None
    provider: Literal["google", "apple", "osm"] | None
    latitude: Decimal | None
    longitude: Decimal | None
    resolution_state: Literal["manual", "parsed", "pending"]
    category: PlaceCategory
    note: str | None
    status: PlaceStatus
    saved_by_user_id: UUID
    wanted_by: list[UUID] = Field(description="Participants who want to go")
    version: int
    created_at: datetime
    updated_at: datetime


class AttendanceResponse(BaseModel):
    participant_id: UUID
    status: Attendance


class ItineraryItemResponse(BaseModel):
    id: UUID
    plan_id: UUID
    title: str
    day: date | None
    start_time: time | None
    timezone: str | None
    duration_minutes: int | None
    note: str | None
    place_id: UUID | None
    lead_participant_id: UUID | None
    booking_id: UUID | None
    status: ItemStatus
    order_key: str
    attendance: list[AttendanceResponse]
    commitment_id: UUID | None = Field(
        description="The estimated cost (a synced cost_commitment); create the expense that "
        "pays it with this commitment_id so budgets count it once"
    )
    created_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


PollKind = Literal["single_choice", "yes_no"]
PollOutcomeAction = Literal["save_place", "add_to_plan"]
OptionLabel = Annotated[str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)]


class PollOptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: OptionLabel
    place_id: UUID | None = None


class PollCreateRequest(BaseModel):
    """A single-choice poll lists 2 to 20 options; a yes/no poll lists none (Yes and No are given).

    Everyone active in the trip when it opens may vote, and only they.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    kind: PollKind = "single_choice"
    question: Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
    options: list[PollOptionRequest] = Field(default_factory=list, max_length=20)
    deadline_at: datetime | None = Field(default=None, description="Closes on its own then")
    quorum: int | None = Field(
        default=None, ge=1, le=100, description="Yes/no only: yes votes needed to pass"
    )
    allow_vote_change: bool = True

    @model_validator(mode="after")
    def _shape(self) -> PollCreateRequest:
        if self.kind == "single_choice" and not 2 <= len(self.options) <= 20:
            raise ValueError("a single-choice poll needs 2 to 20 options")
        if self.kind == "yes_no" and self.options:
            raise ValueError("a yes/no poll has its own Yes and No options")
        if self.kind == "single_choice" and self.quorum is not None:
            raise ValueError("quorum applies to yes/no polls")
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at must include a UTC offset")
        return self


class VoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: UUID


class PollOutcomeRequest(BaseModel):
    """Act on a closed single-choice poll's result.

    ``option_id`` is needed only to pick among tied options; ``day``, ``start_time``,
    and ``timezone`` place the item for ``add_to_plan``.
    """

    model_config = ConfigDict(extra="forbid")

    action: PollOutcomeAction
    result_version: Literal[1] = 1
    option_id: UUID | None = None
    day: date | None = None
    start_time: time | None = None
    timezone: TimezoneName | None = None

    @model_validator(mode="after")
    def _time(self) -> PollOutcomeRequest:
        if self.start_time is not None and (self.day is None or self.start_time.tzinfo is not None):
            raise ValueError("start_time is a local time on a day")
        if self.timezone is not None and self.start_time is None:
            raise ValueError("timezone belongs to start_time")
        return self


class PollOptionResponse(BaseModel):
    id: UUID
    label: str
    place_id: UUID | None
    answer: Literal["yes", "no"] | None
    position: int
    voter_ids: list[UUID] = Field(description="Participants who chose this option")


class PollResultResponse(BaseModel):
    version: int
    outcome: Literal["winner", "tie", "no_votes", "passed", "failed"]
    winner_option_id: UUID | None
    tied_option_ids: list[UUID]
    counts: dict[str, int]
    eligible: int
    voted: int
    closed_at: datetime
    closed_by_user_id: UUID | None = Field(description="Null when the deadline closed it")


class PollOutcomeResponse(BaseModel):
    action: PollOutcomeAction
    option_id: UUID
    created_entity_id: UUID = Field(description="The place or itinerary item it made")


class PollResponse(BaseModel):
    id: UUID
    plan_id: UUID
    kind: PollKind
    question: str
    options: list[PollOptionResponse]
    deadline_at: datetime | None
    quorum: int | None
    allow_vote_change: bool
    status: Literal["open", "closed"]
    eligible: int = Field(description="Participants who may vote")
    result: PollResultResponse | None
    outcomes: list[PollOutcomeResponse]
    created_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


BookingKind = Literal[
    "flight", "lodging", "transport", "activity", "restaurant", "insurance", "other"
]
BookingStatus = Literal["planned", "confirmed", "cancelled"]
PaymentNote = Literal["prepaid", "pay_at_property", "each_paid_own", "personal"]
SecretText = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
PrivateNote = Annotated[
    str, StringConstraints(min_length=1, max_length=2000, strip_whitespace=True)
]


class BookingSecretsRequest(BaseModel):
    """The secrets to change: a value sets one, null clears it, and one left out stays."""

    model_config = ConfigDict(extra="forbid")

    confirmation_code: SecretText | None = None
    private_notes: PrivateNote | None = None


class BookingRequest(BaseModel):
    """A reservation. Start and end are local dates, with an optional time in a zone.

    ``secrets`` is write-only: omit it to keep what is stored. ``price`` is estimated
    while planned and committed once confirmed; create the expense that pays it with
    the booking's ``commitment_id`` so budgets count it once.
    """

    model_config = ConfigDict(extra="forbid")

    kind: BookingKind
    title: Title
    provider: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    start_date: date | None = None
    start_time: time | None = None
    start_timezone: TimezoneName | None = None
    end_date: date | None = None
    end_time: time | None = None
    end_timezone: TimezoneName | None = None
    place_id: UUID | None = None
    traveler_ids: list[UUID] = Field(default_factory=list, max_length=50)
    status: BookingStatus = "planned"
    payment_note: PaymentNote | None = None
    free_cancellation_until: datetime | None = None
    price: EstimatedCost | None = None
    secrets: BookingSecretsRequest | None = None

    @model_validator(mode="after")
    def _times(self) -> BookingRequest:
        for day, moment, zone, name in (
            (self.start_date, self.start_time, self.start_timezone, "start"),
            (self.end_date, self.end_time, self.end_timezone, "end"),
        ):
            if moment is not None and (day is None or zone is None or moment.tzinfo is not None):
                raise ValueError(f"{name}_time is a local time on {name}_date in {name}_timezone")
            if zone is not None and moment is None:
                raise ValueError(f"{name}_timezone belongs to {name}_time")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        if self.free_cancellation_until is not None and self.free_cancellation_until.tzinfo is None:
            raise ValueError("free_cancellation_until must include a UTC offset")
        return self


class BookingCreateRequest(BookingRequest):
    id: UUID | None = None


class BookingResponse(BaseModel):
    id: UUID
    plan_id: UUID
    kind: BookingKind
    title: str
    provider: str | None
    start_date: date | None
    start_time: time | None
    start_timezone: str | None
    end_date: date | None
    end_time: time | None
    end_timezone: str | None
    place_id: UUID | None
    traveler_ids: list[UUID]
    status: BookingStatus
    payment_note: PaymentNote | None
    free_cancellation_until: datetime | None
    has_confirmation_code: bool
    has_private_notes: bool
    commitment_id: UUID | None = Field(
        description="The price (a synced cost_commitment); pay it with an expense naming it"
    )
    created_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class BookingSecretsResponse(BaseModel):
    """Plaintext, for travelers, whoever added the booking, and organisers only."""

    confirmation_code: str | None
    private_notes: str | None
