"""Plans, stable participants, invites, recurring series, and the travel extension."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from sqlalchemy import ARRAY, DateTime, LargeBinary, Text, Time, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class PlanSeries(Base):
    __tablename__ = "plan_series"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    group_id: Mapped[UUID | None]
    created_by_user_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    base_currency: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location_label: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date]
    local_start_time: Mapped[time | None] = mapped_column(Time(timezone=False))
    duration_minutes: Mapped[int | None]
    recurrence_rule: Mapped[str] = mapped_column(Text)
    participant_user_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid(as_uuid=True)))
    horizon_days: Mapped[int]
    materialized_through: Mapped[date | None]
    state: Mapped[str] = mapped_column(Text)
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    group_id: Mapped[UUID | None]
    series_id: Mapped[UUID | None]
    occurrence_key: Mapped[str | None] = mapped_column(Text)
    is_series_exception: Mapped[bool]
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    timing_mode: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    timezone: Mapped[str | None] = mapped_column(Text)
    base_currency: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location_label: Mapped[str | None] = mapped_column(Text)
    duplicated_from_plan_id: Mapped[UUID | None]
    created_by_user_id: Mapped[UUID]
    deletion_scheduled_at: Mapped[datetime | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PlanParticipant(Base):
    """Stable plan-scoped identity; IDs never change through leave, removal, or claim."""

    __tablename__ = "plan_participants"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    identity_kind: Mapped[str] = mapped_column(Text)
    user_id: Mapped[UUID | None]
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    access_state: Mapped[str] = mapped_column(Text)
    rsvp_status: Mapped[str] = mapped_column(Text)
    rsvp_updated_at: Mapped[datetime | None]
    merged_into_participant_id: Mapped[UUID | None]
    joined_via_invite_id: Mapped[UUID | None]
    added_by_user_id: Mapped[UUID | None]
    joined_at: Mapped[datetime | None]
    left_at: Mapped[datetime | None]
    removed_at: Mapped[datetime | None]
    claimed_at: Mapped[datetime | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PlanInvite(Base):
    __tablename__ = "plan_invites"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    purpose: Mapped[str] = mapped_column(Text)
    target_participant_id: Mapped[UUID | None]
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    role: Mapped[str] = mapped_column(Text)
    allow_guests: Mapped[bool]
    requires_approval: Mapped[bool]
    intended_email_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    max_uses: Mapped[int | None]
    use_count: Mapped[int]
    expires_at: Mapped[datetime]
    state: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime | None]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class TravelPlanDetails(Base):
    __tablename__ = "travel_plan_details"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    destination_summary: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class TravelSegment(Base):
    __tablename__ = "travel_segments"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    segment_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    origin_label: Mapped[str | None] = mapped_column(Text)
    destination_label: Mapped[str | None] = mapped_column(Text)
    timing_mode: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    departure_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    departure_timezone: Mapped[str | None] = mapped_column(Text)
    departs_at: Mapped[datetime | None]
    arrival_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    arrival_timezone: Mapped[str | None] = mapped_column(Text)
    arrives_at: Mapped[datetime | None]
    sort_order: Mapped[int]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]
