"""Trip planning: bookings, and their secrets sealed at rest."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from sqlalchemy import ARRAY, LargeBinary, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "bookings"}


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None]
    start_time: Mapped[time | None]
    start_timezone: Mapped[str | None] = mapped_column(Text)
    end_date: Mapped[date | None]
    end_time: Mapped[time | None]
    end_timezone: Mapped[str | None] = mapped_column(Text)
    place_id: Mapped[UUID | None]
    traveler_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)))
    status: Mapped[str] = mapped_column(Text)
    payment_note: Mapped[str | None] = mapped_column(Text)
    free_cancellation_until: Mapped[datetime | None]
    has_confirmation_code: Mapped[bool]
    has_private_notes: Mapped[bool]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class BookingSecret(Base):
    """Ciphertexts only (``beluno.secret_box``); readable by those who may reveal them."""

    __tablename__ = "booking_secrets"
    __table_args__ = SCHEMA

    booking_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    key_id: Mapped[str] = mapped_column(Text)
    confirmation_code: Mapped[bytes | None] = mapped_column(LargeBinary)
    private_notes: Mapped[bytes | None] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime]
