"""Plans, stable participants, and invites."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = {"schema": "plans"}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    activity: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    timing_mode: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    timezone: Mapped[str | None] = mapped_column(Text)
    base_currency: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location_label: Mapped[str | None] = mapped_column(Text)
    destinations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    pass_color: Mapped[str] = mapped_column(Text)
    expected_size: Mapped[int | None]
    cover_media_id: Mapped[UUID | None]
    album_url: Mapped[str | None] = mapped_column(Text)
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
    default_share: Mapped[int]
    avatar_color: Mapped[str] = mapped_column(Text)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(Text))
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
