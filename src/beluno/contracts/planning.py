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
