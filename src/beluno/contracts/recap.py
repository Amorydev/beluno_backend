"""A plan's recap: what the group did together, and the fields safe for a public card."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field

from beluno.contracts.finance import ExpenseCategory


class RecapStop(BaseModel):
    name: str
    code: str | None
    nights: int | None = Field(description="From the stop's dates, when both are set")


class RecapCategory(BaseModel):
    category: ExpenseCategory
    spent_minor: int
    share_basis_points: int = Field(
        description="Of the total spent, in 1/100 of a percent; all categories add up to 10000"
    )


class RecapTopPlace(BaseModel):
    place_id: UUID
    name: str
    wanted_by: int
    of_people: int = Field(description="Active participants who can answer (not placeholders)")


class RecapPerson(BaseModel):
    participant_id: UUID
    active: bool = Field(description="False: left or was removed with money still open")
    settled: bool = Field(
        description="Square by the ledger's rule (base-currency balances within the tolerance)"
    )


class RecapShareCard(BaseModel):
    """Only these fields may go on a public card. Names, balances, booking codes,
    addresses, and notes never do; the app draws the card on the device."""

    route: list[str]
    start_date: date | None
    days: int | None
    people: int
    cover_media_id: UUID | None = Field(description="The trip's cover photo, if set")
    currency: str
    spent_minor: int = Field(description="Shown only when the person chooses to")
    spent_complete: bool = Field(
        description="False when some spending had no rate to the base currency; hide the total"
    )


class RecapResponse(BaseModel):
    plan_id: UUID
    title: str
    start_date: date | None = Field(description="Local first day (also for timed plans)")
    end_date: date | None = Field(description="Local last day")
    days: int | None
    stops: list[RecapStop]
    people: int = Field(description="Active participants, placeholders included")
    currency: str = Field(description="The plan's base currency")
    spent_minor: int = Field(description="Paid expenses net of refunds, in the base currency")
    per_person_per_day_minor: int | None
    unconverted: bool = Field(description="Some spending had no rate to the base currency")
    estimated_rates: bool = Field(description="Some conversions used estimated rates")
    categories: list[RecapCategory]
    top_place: RecapTopPlace | None
    itinerary_done: int
    itinerary_total: int = Field(description="Items not cancelled")
    polls_decided: int
    crew: list[RecapPerson]
    all_settled: bool = Field(description="The ledger's status is settled")
    settled_on: date | None = Field(
        description="Once settled: the date of the last settlement still standing, as entered"
    )
    cover_media_id: UUID | None
    highlights: list[UUID] = Field(
        description="Ready memories an organiser picked for the recap, by day and time"
    )
    memories: int = Field(description="Ready memories of the trip")
    share: RecapShareCard
