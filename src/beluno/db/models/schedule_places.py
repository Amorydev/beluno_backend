"""Trip planning: saved places, the itinerary, and each participant's answers."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "schedule_places"}


class Place(Base):
    __tablename__ = "places"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(Text)
    maps_url: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    resolution_state: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    saved_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class PlaceReaction(Base):
    """One participant's "want to go"; updated in place, never deleted."""

    __tablename__ = "place_reactions"
    __table_args__ = SCHEMA

    place_id: Mapped[UUID] = mapped_column(primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    wants: Mapped[bool]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ItineraryItem(Base):
    __tablename__ = "itinerary_items"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    day: Mapped[date | None]
    start_time: Mapped[time | None]
    timezone: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int | None]
    title: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    place_id: Mapped[UUID | None]
    lead_participant_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(Text)
    order_key: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class ItemAttendance(Base):
    """One participant's "going / not going" for an itinerary item."""

    __tablename__ = "item_attendance"
    __table_args__ = SCHEMA

    item_id: Mapped[UUID] = mapped_column(primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
